# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import abc
import math
import queue
import time
from collections import OrderedDict
from copy import deepcopy
from multiprocessing import connection, queues
from textwrap import indent
from typing import Callable, Iterator, Optional, Sequence, Tuple, Union

import numpy as np
import torch
from torch import multiprocessing as mp
from torch.utils.data import IterableDataset

from torchrl.envs.utils import set_exploration_mode, step_tensordict
from torchrl.modules import ProbabilisticTDModule
from .utils import split_trajectories

__all__ = [
    "SyncDataCollector",
    "aSyncDataCollector",
    "MultiaSyncDataCollector",
    "MultiSyncDataCollector",
]

from torchrl.envs.transforms import TransformedEnv
from ..data import TensorSpec
from ..data.tensordict.tensordict import _TensorDict, TensorDict
from ..data.utils import CloudpickleWrapper, DEVICE_TYPING
from ..envs.common import _EnvClass
from ..envs.vec_env import _BatchedEnv

_TIMEOUT = 1.0
_MIN_TIMEOUT = 1e-3  # should be several orders of magnitude inferior wrt time spent collecting a trajectory


class RandomPolicy:
    def __init__(self, action_spec: TensorSpec):
        """Random policy for a given action_spec.
        This is a wrapper around the action_spec.rand method.


        $ python example_google.py

        Args:
            action_spec: TensorSpec object describing the action specs

        Examples:
            >>> from torchrl.data.tensor_specs import NdBoundedTensorSpec
            >>> from torchrl.data.tensordict import TensorDict
            >>> action_spec = NdBoundedTensorSpec(-torch.ones(3), torch.ones(3))
            >>> actor = RandomPolicy(spec=action_spec)
            >>> td = actor(TensorDict(batch_size=[])) # selects a random action in the cube [-1; 1]
        """
        self.action_spec = action_spec

    def __call__(self, td: _TensorDict) -> _TensorDict:
        return td.set("action", self.action_spec.rand(td.batch_size))


def recursive_map_to_cpu(dictionary: OrderedDict) -> OrderedDict:
    return OrderedDict(
        **{
            k: recursive_map_to_cpu(item)
            if isinstance(item, OrderedDict)
            else item.cpu()
            for k, item in dictionary.items()
        }
    )


class _DataCollector(IterableDataset, metaclass=abc.ABCMeta):
    def _get_policy_and_device(
        self,
        create_env_fn: Optional[
            Union[_EnvClass, "EnvCreator", Sequence[Callable[[], _EnvClass]]]
        ] = None,
        create_env_kwargs: Optional[dict] = None,
        policy: Optional[
            Union[ProbabilisticTDModule, Callable[[_TensorDict], _TensorDict]]
        ] = None,
        device: Optional[DEVICE_TYPING] = None,
    ) -> Tuple[ProbabilisticTDModule, torch.device, Union[None, Callable[[], dict]]]:
        """From a policy and a device, assigns the self.device attribute to
        the desired device and maps the policy onto it or (if the device is
        ommitted) assigns the self.device attribute to the policy device.

        Args:
            create_env_fn (Callable or list of callables): an env creator
                function (or a list of creators)
            create_env_kwargs (dictionary): kwargs for the env creator
            policy (ProbabilisticTDModule, optional): a policy to be used
            device (int, str or torch.device, optional): device where to place
                the policy

        """
        if create_env_fn is not None:
            if create_env_kwargs is None:
                create_env_kwargs = dict()
            self.create_env_fn = create_env_fn
            if isinstance(create_env_fn, _EnvClass):
                env = create_env_fn
            else:
                env = self.create_env_fn(**create_env_kwargs)
        else:
            env = None

        if policy is None:
            if env is None:
                raise ValueError(
                    "env must be provided to _get_policy_and_device if policy is None"
                )
            policy = RandomPolicy(env.action_spec)
        try:
            policy_device = next(policy.parameters()).device
        except:  # noqa
            policy_device = (
                torch.device(device) if device is not None else torch.device("cpu")
            )

        device = torch.device(device) if device is not None else policy_device
        if device is None:
            # if device cannot be found in policy and is not specified, set cpu
            device = torch.device("cpu")
        get_weights_fn = None
        if policy_device != device:
            get_weights_fn = policy.state_dict
            policy = deepcopy(policy).requires_grad_(False).to(device)
            policy.share_memory()
            # if not (len(list(policy.parameters())) == 0 or next(policy.parameters()).is_shared()):
            #     raise RuntimeError("Provided policy parameters must be shared.")
        if hasattr(env, "close") and not env.is_closed:
            env.close()
        return policy, device, get_weights_fn

    def update_policy_weights_(self) -> None:
        """Update the policy weights if the policy of the data collector and the trained policy live on different devices."""
        if self.get_weights_fn is not None:
            self.policy.load_state_dict(self.get_weights_fn())

    def __iter__(self) -> Iterator[_TensorDict]:
        return self.iterator()

    @abc.abstractmethod
    def iterator(self) -> Iterator[_TensorDict]:
        raise NotImplementedError

    @abc.abstractmethod
    def set_seed(self, seed: int) -> int:
        raise NotImplementedError

    @abc.abstractmethod
    def state_dict(self) -> OrderedDict:
        raise NotImplementedError

    @abc.abstractmethod
    def load_state_dict(self, state_dict: OrderedDict) -> None:
        raise NotImplementedError

    def __repr__(self) -> str:
        string = f"{self.__class__.__name__}()"
        return string


class SyncDataCollector(_DataCollector):
    """
    Generic data collector for RL problems. Requires and environment constructor and a policy.

    Args:
        create_env_fn (Callable), returns an instance of _EnvClass class.
        policy (Callable, optional): Policy to be executed in the environment.
            Must accept _TensorDict object as input.
        total_frames (int): lower bound of the total number of frames returned by the collector. The iterator will
            stop once the total number of frames equates or exceeds the total number of frames passed to the
            collector.
        create_env_kwargs (dict, optional): Dictionary of kwargs for create_env_fn.
        max_frames_per_traj (int, optional): Maximum steps per trajectory. Note that a trajectory can span over multiple batches
            (unless reset_at_each_iter is set to True, see below). Once a trajectory reaches n_steps_max,
            the environment is reset. If the environment wraps multiple environments together, the number of steps
            is tracked for each environment independently. Negative values are allowed, in which case this argument
            is ignored.
            default: -1 (i.e. no maximum number of steps)
        frames_per_batch (int): Time-length of a batch.
            reset_at_each_iter and frames_per_batch == n_steps_max are equivalent configurations.
            default: 200
        init_random_frames (int, optional): Number of frames for which the policy is ignored before it is called.
            This feature is mainly intended to be used in offline/model-based settings, where a batch of random
            trajectories can be used to initialize training.
            default=-1 (i.e. no random frames)
        reset_at_each_iter (bool): Whether or not environments should be reset for each batch.
            default=False.
        postproc (Callable, optional): A Batcher is an object that will read a batch of data and return it in a useful format for training.
            default: None.
        split_trajs (bool): Boolean indicating whether the resulting TensorDict should be split according to the trajectories.
            See utils.split_trajectories for more information.
        device (int, str or torch.device, optional): The device on which the policy will be placed.
            If it differs from the input policy device, the update_policy_weights_() method should be queried
            at appropriate times during the training loop to accommodate for the lag between parameter configuration
            at various times.
            default = None (i.e. policy is kept on its original device)
        seed (int, optional): seed to be used for torch and numpy.
        pin_memory (bool): whether pin_memory() should be called on the outputs.
        passing_device (int, str or torch.device, optional): The device on which the output TensorDict will be stored.
            For long trajectories, it may be necessary to store the data on a different device than the one where
            the policy is stored.
            default = "cpu"
        return_in_place (bool): if True, the collector will yield the same tensordict container with updated values
            at each iteration.
            default = False
        exploration_mode (str, optional): interaction mode to be used when collecting data. Must be one of "random",
            "mode", "mean" or "net_output".
            default = "random"
        init_with_lag (bool, optional): if True, the first trajectory will be truncated earlier at a random step.
            This is helpful to desynchronize the environments, such that steps do no match in all collected rollouts.
            default = True
        return_same_td (bool, optional): if True, the same TensorDict will be returned at each iteration, with its values
            updated. This feature should be used cautiously: if the same tensordict is added to a replay buffer for instance,
            the whole content of the buffer will be identical.
            Default is False.
    """

    def __init__(
        self,
        create_env_fn: Union[
            _EnvClass, "EnvCreator", Sequence[Callable[[], _EnvClass]]
        ],
        policy: Optional[
            Union[ProbabilisticTDModule, Callable[[_TensorDict], _TensorDict]]
        ] = None,
        total_frames: Optional[int] = -1,
        create_env_kwargs: Optional[dict] = None,
        max_frames_per_traj: int = -1,
        frames_per_batch: int = 200,
        init_random_frames: int = -1,
        reset_at_each_iter: bool = False,
        postproc: Optional[Callable[[_TensorDict], _TensorDict]] = None,
        split_trajs: bool = True,
        device: DEVICE_TYPING = None,
        passing_device: DEVICE_TYPING = "cpu",
        seed: Optional[int] = None,
        pin_memory: bool = False,
        return_in_place: bool = False,
        exploration_mode: str = "random",
        init_with_lag: bool = False,
        return_same_td: bool = False,
    ):
        self.closed = True
        if seed is not None:
            torch.manual_seed(seed)
            np.random.seed(seed)

        if create_env_kwargs is None:
            create_env_kwargs = {}
        if not isinstance(create_env_fn, _EnvClass):
            env = create_env_fn(**create_env_kwargs)
        else:
            env = create_env_fn
            if create_env_kwargs:
                if not isinstance(env, _BatchedEnv):
                    raise RuntimeError(
                        "kwargs were passed to SyncDataCollector but they can't be set "
                        f"on environment of type {type(create_env_fn)}."
                    )
                env.update_kwargs(create_env_kwargs)

        self.env: _EnvClass = env
        self.closed = False
        self.n_env = self.env.numel()

        (self.policy, self.device, self.get_weights_fn,) = self._get_policy_and_device(
            create_env_fn=create_env_fn,
            create_env_kwargs=create_env_kwargs,
            policy=policy,
            device=device,
        )

        self.env_device = env.device
        if not total_frames > 0:
            total_frames = float("inf")
        self.total_frames = total_frames
        self.reset_at_each_iter = reset_at_each_iter
        self.init_random_frames = init_random_frames
        self.postproc = postproc
        if self.postproc is not None:
            self.postproc.to(self.passing_device)
        self.max_frames_per_traj = max_frames_per_traj
        self.frames_per_batch = -(-frames_per_batch // self.n_env)
        self.pin_memory = pin_memory
        self.exploration_mode = exploration_mode
        self.init_with_lag = init_with_lag and max_frames_per_traj > 0
        self.return_same_td = return_same_td

        self.passing_device = torch.device(passing_device)

        env.reset()
        self._tensordict = env.current_tensordict.to(self.passing_device)
        self._tensordict.set(
            "step_count", torch.zeros(*self.env.batch_size, 1, dtype=torch.int)
        )
        self._tensordict_out = TensorDict(
            {},
            batch_size=[*self.env.batch_size, self.frames_per_batch],
            device=self.passing_device,
        )

        self.return_in_place = return_in_place
        self.split_trajs = split_trajs
        if self.return_in_place and self.split_trajs:
            raise RuntimeError(
                "the 'return_in_place' and 'split_trajs' argument are incompatible, but found to be both "
                "True. split_trajs=True will cause the output tensordict to have an unpredictable output "
                "shape, which prevents caching and overwriting the tensors."
            )
        self._td_env = None
        self._td_policy = None
        self._has_been_done = None
        self._exclude_private_keys = True

    def set_seed(self, seed: int) -> int:
        """Sets the seeds of the environments stored in the DataCollector.

        Args:
            seed (int): integer representing the seed to be used for the environment.

        Returns:
            Output seed. This is useful when more than one environment is contained in the DataCollector, as the
            seed will be incremented for each of these. The resulting seed is the seed of the last environment.

        Examples:
            >>> env_fn = lambda: GymEnv("Pendulum-v1")
            >>> env_fn_parallel = lambda: ParallelEnv(6, env_fn)
            >>> collector = SyncDataCollector(env_fn_parallel)
            >>> out_seed = collector.set_seed(1)  # out_seed = 6

        """
        return self.env.set_seed(seed)

    def iterator(self) -> Iterator[_TensorDict]:
        """Iterates through the DataCollector.

        Yields: _TensorDict objects containing (chunks of) trajectories

        """
        total_frames = self.total_frames
        i = -1
        self._frames = 0
        while True:
            i += 1
            self._iter = i
            tensordict_out = self.rollout()
            self._frames += tensordict_out.numel()
            if self._frames >= total_frames:
                self.env.close()

            if self.split_trajs:
                tensordict_out = split_trajectories(tensordict_out)
            if self.postproc is not None:
                tensordict_out = self.postproc(tensordict_out)
            if self._exclude_private_keys:
                excluded_keys = [
                    key for key in tensordict_out.keys() if key.startswith("_")
                ]
                tensordict_out = tensordict_out.exclude(*excluded_keys, inplace=True)
            if self.return_same_td:
                yield tensordict_out
            else:
                yield tensordict_out.clone()

            del tensordict_out
            if self._frames >= self.total_frames:
                break

    def _cast_to_policy(self, td: _TensorDict) -> _TensorDict:
        policy_device = self.device
        if hasattr(self.policy, "in_keys"):
            td = td.select(*self.policy.in_keys)
        if self._td_policy is None:
            self._td_policy = td.to(policy_device)
        else:
            if td.device == torch.device("cpu") and self.pin_memory:
                td.pin_memory()
            self._td_policy.update(td, inplace=True)
        return self._td_policy

    def _cast_to_env(
        self, td: _TensorDict, dest: Optional[_TensorDict] = None
    ) -> _TensorDict:
        env_device = self.env_device
        if dest is None:
            if self._td_env is None:
                self._td_env = td.to(env_device)
            else:
                self._td_env.update(td, inplace=True)
            return self._td_env
        else:
            return dest.update(td, inplace=True)

    def _reset_if_necessary(self) -> None:
        done = self._tensordict.get("done")
        steps = self._tensordict.get("step_count")
        done_or_terminated = done | (steps == self.max_frames_per_traj)
        if self._has_been_done is None:
            self._has_been_done = done_or_terminated
        else:
            self._has_been_done = self._has_been_done | done_or_terminated
        if not self._has_been_done.all() and self.init_with_lag:
            _reset = torch.zeros_like(done_or_terminated).bernoulli_(
                1 / self.max_frames_per_traj
            )
            _reset[self._has_been_done] = False
            done_or_terminated = done_or_terminated | _reset
        if done_or_terminated.any():
            traj_ids = self._tensordict.get("traj_ids").clone()
            steps = steps.clone()
            if len(self.env.batch_size):
                self._tensordict.masked_fill_(done_or_terminated.squeeze(-1), 0)
                self._tensordict.set("reset_workers", done_or_terminated)
            else:
                self._tensordict.zero_()
            self.env.reset()
            self._tensordict.update(self.env.current_tensordict)
            if self._tensordict.get("done").any():
                raise RuntimeError(
                    f"Got {sum(self._tensordict.get('done'))} done envs after reset."
                )
            if len(self.env.batch_size):
                self._tensordict.del_("reset_workers")
            traj_ids[done_or_terminated] = traj_ids.max() + torch.arange(
                1, done_or_terminated.sum() + 1, device=traj_ids.device
            )
            steps[done_or_terminated] = 0
            self._tensordict.set("traj_ids", traj_ids)  # no ops if they already match
            self._tensordict.set("step_count", steps)

    @torch.no_grad()
    def rollout(self) -> _TensorDict:
        """Computes a rollout in the environment using the provided policy.

        Returns:
            _TensorDict containing the computed rollout.

        """
        if self.reset_at_each_iter:
            self.env.reset()
            self._tensordict.update(self.env.current_tensordict)
            self._tensordict.fill_("step_count", 0)

        n = self.env.batch_size[0] if len(self.env.batch_size) else 1
        self._tensordict.set("traj_ids", torch.arange(n).unsqueeze(-1))

        tensordict_out = []
        with set_exploration_mode(self.exploration_mode):
            for t in range(self.frames_per_batch):
                if self._frames < self.init_random_frames:
                    self.env.rand_step(self._tensordict)
                else:
                    td_cast = self._cast_to_policy(self._tensordict)
                    td_cast = self.policy(td_cast)
                    self._cast_to_env(td_cast, self._tensordict)
                    self.env.step(self._tensordict)

                step_count = self._tensordict.get("step_count")
                step_count += 1
                tensordict_out.append(self._tensordict.clone())

                self._reset_if_necessary()
                self._tensordict.update(step_tensordict(self._tensordict))
            if self.return_in_place and len(self._tensordict_out.keys()) > 0:
                tensordict_out = torch.stack(tensordict_out, len(self.env.batch_size))
                tensordict_out = tensordict_out.select(*self._tensordict_out.keys())
                return self._tensordict_out.update_(tensordict_out)
        return torch.stack(
            tensordict_out,
            len(self.env.batch_size),
            out=self._tensordict_out,
        )  # dim 0 for single env, dim 1 for batch

    def reset(self, index=None, **kwargs) -> None:
        """Resets the environments to a new initial state."""
        if index is not None:
            # check that the env supports partial reset
            if np.prod(self.env.batch_size) == 0:
                raise RuntimeError("resetting unique env with index is not permitted.")
            reset_workers = torch.zeros(
                *self.env.batch_size,
                1,
                dtype=torch.bool,
                device=self.env.device,
            )
            reset_workers[index] = 1
            td_in = TensorDict({"reset_workers": reset_workers}, self.env.batch_size)
            self._tensordict[index].zero_()
        else:
            td_in = None
            self._tensordict.zero_()

        if td_in:
            self._tensordict.update(td_in)
        self.env.reset(**kwargs)

        self._tensordict.update(self.env.current_tensordict)
        self._tensordict.fill_("step_count", 0)

    def shutdown(self) -> None:
        """Shuts down all workers and/or closes the local environment."""
        if not self.closed:
            self.closed = True
            del self._tensordict, self._tensordict_out
            if not self.env.is_closed:
                self.env.close()
            del self.env

    def __del__(self):
        self.shutdown()  # make sure env is closed

    def state_dict(self) -> OrderedDict:
        """Returns the local state_dict of the data collector (environment
        and policy).

        Returns:
            an ordered dictionary with fields `"policy_state_dict"` and
            `"env_state_dict"`.

        """

        if isinstance(self.env, TransformedEnv):
            env_state_dict = self.env.transform.state_dict()
        elif isinstance(self.env, _BatchedEnv):
            env_state_dict = self.env.state_dict()
        else:
            env_state_dict = OrderedDict()

        if hasattr(self.policy, "state_dict"):
            policy_state_dict = self.policy.state_dict()
            state_dict = OrderedDict(
                policy_state_dict=policy_state_dict,
                env_state_dict=env_state_dict,
            )
        else:
            state_dict = OrderedDict(env_state_dict=env_state_dict)

        return state_dict

    def load_state_dict(self, state_dict: OrderedDict, **kwargs) -> None:
        """Loads a state_dict on the environment and policy.

        Args:
            state_dict (OrderedDict): ordered dictionary containing the fields
                `"policy_state_dict"` and `"env_state_dict"`.

        """
        strict = kwargs.get("strict", True)
        if strict or "env_state_dict" in state_dict:
            self.env.load_state_dict(state_dict["env_state_dict"], **kwargs)
        if strict or "policy_state_dict" in state_dict:
            self.policy.load_state_dict(state_dict["policy_state_dict"], **kwargs)

    def __repr__(self) -> str:
        env_str = indent(f"env={self.env}", 4 * " ")
        policy_str = indent(f"policy={self.policy}", 4 * " ")
        td_out_str = indent(f"td_out={self._tensordict_out}", 4 * " ")
        string = f"{self.__class__.__name__}(\n{env_str},\n{policy_str},\n{td_out_str})"
        return string


class _MultiDataCollector(_DataCollector):
    """Runs a given number of DataCollectors on separate processes.

    Args:
        create_env_fn (list of Callabled): list of Callables, each returning an instance of _EnvClass
        policy (Callable, optional): Instance of ProbabilisticTDModule class. Must accept _TensorDict object as input.
        total_frames (int): lower bound of the total number of frames returned by the collector. In parallel settings,
            the actual number of frames may well be greater than this as the closing signals are sent to the
            workers only once the total number of frames has been collected on the server.
        create_env_kwargs (dict, optional): A (list of) dictionaries with the arguments used to create an environment
        max_frames_per_traj: Maximum steps per trajectory. Note that a trajectory can span over multiple batches
            (unless reset_at_each_iter is set to True, see below). Once a trajectory reaches n_steps_max,
            the environment is reset. If the environment wraps multiple environments together, the number of steps
            is tracked for each environment independently. Negative values are allowed, in which case this argument
            is ignored.
            default: -1 (i.e. no maximum number of steps)
        frames_per_batch (int): Time-length of a batch.
            reset_at_each_iter and frames_per_batch == n_steps_max are equivalent configurations.
            default: 200
        init_random_frames (int): Number of frames for which the policy is ignored before it is called.
            This feature is mainly intended to be used in offline/model-based settings, where a batch of random
            trajectories can be used to initialize training.
            default=-1 (i.e. no random frames)
        reset_at_each_iter (bool): Whether or not environments should be reset for each batch.
            default=False.
        postproc (callable, optional): A PostProcessor is an object that will read a batch of data and process it in a
            useful format for training.
            default: None.
        split_trajs (bool): Boolean indicating whether the resulting TensorDict should be split according to the trajectories.
            See utils.split_trajectories for more information.
        devices (int, str, torch.device or sequence of such, optional): The devices on which the policy will be placed.
            If it differs from the input policy device, the update_policy_weights_() method should be queried
            at appropriate times during the training loop to accommodate for the lag between parameter configuration
            at various times.
            default = None (i.e. policy is kept on its original device)
        passing_devices (int, str, torch.device or sequence of such, optional): The devices on which the output
            TensorDict will be stored. For long trajectories, it may be necessary to store the data on a different
            device than the one where the policy is stored.
            default = "cpu"
        update_at_each_batch (bool): if True, the policy weights will be updated every time a batch of trajectories
            is collected.
            default=False
        init_with_lag (bool, optional): if True, the first trajectory will be truncated earlier at a random step.
            This is helpful to desynchronize the environments, such that steps do no match in all collected rollouts.
            default = True
       exploration_mode (str, optional): interaction mode to be used when collecting data. Must be one of "random",
            "mode", "mean" or "net_output".
            default = "random"

    """

    def __init__(
        self,
        create_env_fn: Sequence[Callable[[], _EnvClass]],
        policy: Optional[
            Union[ProbabilisticTDModule, Callable[[_TensorDict], _TensorDict]]
        ] = None,
        total_frames: Optional[int] = -1,
        create_env_kwargs: Optional[Sequence[dict]] = None,
        max_frames_per_traj: int = -1,
        frames_per_batch: int = 200,
        init_random_frames: int = -1,
        reset_at_each_iter: bool = False,
        postproc: Optional[Callable[[_TensorDict], _TensorDict]] = None,
        split_trajs: bool = True,
        devices: DEVICE_TYPING = None,
        seed: Optional[int] = None,
        pin_memory: bool = False,
        passing_devices: Union[DEVICE_TYPING, Sequence[DEVICE_TYPING]] = "cpu",
        update_at_each_batch: bool = False,
        init_with_lag: bool = False,
        exploration_mode: str = "random",
    ):
        self.closed = True
        self.create_env_fn = create_env_fn
        self.num_workers = len(create_env_fn)
        self.create_env_kwargs = (
            create_env_kwargs
            if create_env_kwargs is not None
            else [dict() for _ in range(self.num_workers)]
        )
        # Preparing devices:
        # We want the user to be able to choose, for each worker, on which
        # device will the policy live and which device will be used to store
        # data. Those devices may or may not match.
        # One caveat is that, if there is only one device for the policy, and
        # if there are multiple workers, sending the same device and policy
        # to be copied to each worker will result in multiple copies of the
        # same policy on the same device.
        # To go around this, we do the copies of the policy in the server
        # (this object) to each possible device, and send to all the
        # processes their copy of the policy.

        def device_err_msg(device_name, devices_list):
            return (
                f"The length of the {device_name} argument should match the "
                f"number of workers of the collector. Got len("
                f"create_env_fn)={self.num_workers} and len("
                f"passing_devices)={len(devices_list)}"
            )

        if isinstance(devices, (str, int, torch.device)):
            devices = [torch.device(devices) for _ in range(self.num_workers)]
        elif devices is None:
            devices = [None for _ in range(self.num_workers)]
        elif isinstance(devices, Sequence):
            if len(devices) != self.num_workers:
                raise RuntimeError(device_err_msg("devices", devices))
            devices = [torch.device(_device) for _device in devices]
        else:
            raise ValueError(
                "devices should be either None, a torch.device or equivalent "
                "or an iterable of devices. "
                f"Found {type(devices)} instead."
            )
        self._policy_dict = {}
        self._get_weights_fn_dict = {}
        for i, _device in enumerate(devices):
            _policy, _device, _get_weight_fn = self._get_policy_and_device(
                policy=policy,
                device=_device,
            )
            if _device not in self._policy_dict:
                self._policy_dict[_device] = _policy
                self._get_weights_fn_dict[_device] = _get_weight_fn
            devices[i] = _device
        self.devices = devices

        if isinstance(passing_devices, (str, int, torch.device)):
            self.passing_devices = [
                torch.device(passing_devices) for _ in range(self.num_workers)
            ]
        elif isinstance(passing_devices, Sequence):
            if len(passing_devices) != self.num_workers:
                raise RuntimeError(device_err_msg("passing_devices", passing_devices))
            self.passing_devices = [
                torch.device(_passing_device) for _passing_device in passing_devices
            ]
        else:
            raise ValueError(
                "passing_devices should be either a torch.device or equivalent or an iterable of devices. "
                f"Found {type(passing_devices)} instead."
            )

        self.total_frames = total_frames if total_frames > 0 else float("inf")
        self.reset_at_each_iter = reset_at_each_iter
        self.postprocs = dict()
        if postproc is not None:
            for _device in self.passing_devices:
                if _device not in self.postprocs:
                    self.postprocs[_device] = deepcopy(postproc).to(_device)
        self.max_frames_per_traj = max_frames_per_traj
        self.frames_per_batch = frames_per_batch
        self.seed = seed
        self.split_trajs = split_trajs
        self.pin_memory = pin_memory
        self.init_random_frames = init_random_frames
        self.update_at_each_batch = update_at_each_batch
        self.init_with_lag = init_with_lag
        self.exploration_mode = exploration_mode
        self.frames_per_worker = np.inf
        self._run_processes()
        self._exclude_private_keys = True

    @property
    def frames_per_batch_worker(self):
        raise NotImplementedError

    def update_policy_weights_(self) -> None:
        for _device in self._policy_dict:
            if self._get_weights_fn_dict[_device] is not None:
                self._policy_dict[_device].load_state_dict(
                    self._get_weights_fn_dict[_device]()
                )

    @property
    def _queue_len(self) -> int:
        raise NotImplementedError

    def _run_processes(self) -> None:
        queue_out = mp.Queue(self._queue_len)  # sends data from proc to main
        self.procs = []
        self.pipes = []
        for i, (env_fun, env_fun_kwargs) in enumerate(
            zip(self.create_env_fn, self.create_env_kwargs)
        ):
            _device = self.devices[i]
            _passing_device = self.passing_devices[i]
            pipe_parent, pipe_child = mp.Pipe()  # send messages to procs
            if env_fun.__class__.__name__ != "EnvCreator" and not isinstance(
                env_fun, _EnvClass
            ):  # to avoid circular imports
                env_fun = CloudpickleWrapper(env_fun)

            kwargs = {
                "pipe_parent": pipe_parent,
                "pipe_child": pipe_child,
                "queue_out": queue_out,
                "create_env_fn": env_fun,
                "create_env_kwargs": env_fun_kwargs,
                "policy": self._policy_dict[_device],
                "frames_per_worker": self.frames_per_worker,
                "max_frames_per_traj": self.max_frames_per_traj,
                "frames_per_batch": self.frames_per_batch_worker,
                "reset_at_each_iter": self.reset_at_each_iter,
                "device": _device,
                "passing_device": _passing_device,
                "seed": self.seed,
                "pin_memory": self.pin_memory,
                "init_with_lag": self.init_with_lag,
                "exploration_mode": self.exploration_mode,
                "idx": i,
            }
            proc = mp.Process(target=_main_async_collector, kwargs=kwargs)
            # proc.daemon can't be set as daemonic processes may be launched by the process itself
            proc.start()
            pipe_child.close()
            self.procs.append(proc)
            self.pipes.append(pipe_parent)
        self.queue_out = queue_out
        self.closed = False

    def __del__(self):
        self.shutdown()

    def shutdown(self) -> None:
        """Shuts down all processes. This operation is irreversible."""
        self._shutdown_main()

    def _shutdown_main(self) -> None:
        if self.closed:
            return
        self.closed = True
        for idx in range(self.num_workers):
            self.pipes[idx].send((None, "close"))

        for idx in range(self.num_workers):
            msg = self.pipes[idx].recv()
            if msg != "closed":
                raise RuntimeError(f"got {msg} but expected 'close'")

        for proc in self.procs:
            proc.join()

        self.queue_out.close()
        for pipe in self.pipes:
            pipe.close()

    def set_seed(self, seed: int) -> int:
        """Sets the seeds of the environments stored in the DataCollector.

        Args:
            seed: integer representing the seed to be used for the environment.

        Returns:
            Output seed. This is useful when more than one environment is
            contained in the DataCollector, as the seed will be incremented for
            each of these. The resulting seed is the seed of the last
            environment.

        Examples:
            >>> env_fn = lambda: GymEnv("Pendulum-v0")
            >>> env_fn_parallel = lambda: ParallelEnv(6, env_fn)
            >>> collector = SyncDataCollector(env_fn_parallel)
            >>> out_seed = collector.set_seed(1)  # out_seed = 6

        """

        for idx in range(self.num_workers):
            self.pipes[idx].send((seed, "seed"))
            new_seed, msg = self.pipes[idx].recv()
            if msg != "seeded":
                raise RuntimeError(f"Expected msg='seeded', got {msg}")
            seed = new_seed
            if idx < self.num_workers - 1:
                seed = seed + 1
        self.reset()
        return seed

    def reset(self, reset_idx: Optional[Sequence[bool]] = None) -> None:
        """Resets the environments to a new initial state.

        Args:
            reset_idx: Optional. Sequence indicating which environments have
                to be reset. If None, all environments are reset.

        """

        if reset_idx is None:
            reset_idx = [True for _ in range(self.num_workers)]
        for idx in range(self.num_workers):
            if reset_idx[idx]:
                self.pipes[idx].send((None, "reset"))
        for idx in range(self.num_workers):
            if reset_idx[idx]:
                j, msg = self.pipes[idx].recv()
                if msg != "reset":
                    raise RuntimeError(f"Expected msg='reset', got {msg}")

    def state_dict(self) -> OrderedDict:
        """
        Returns the state_dict of the data collector.
        Each field represents a worker containing its own state_dict.

        """
        for idx in range(self.num_workers):
            self.pipes[idx].send((None, "state_dict"))
        state_dict = OrderedDict()
        for idx in range(self.num_workers):
            _state_dict, msg = self.pipes[idx].recv()
            if msg != "state_dict":
                raise RuntimeError(f"Expected msg='state_dict', got {msg}")
            state_dict[f"worker{idx}"] = _state_dict

        return state_dict

    def load_state_dict(self, state_dict: OrderedDict) -> None:
        """
        Loads the state_dict on the workers.

        Args:
            state_dict (OrderedDict): state_dict of the form
                ``{"worker0": state_dict0, "worker1": state_dict1}``.

        """

        for idx in range(self.num_workers):
            self.pipes[idx].send((state_dict[f"worker{idx}"], "load_state_dict"))
        for idx in range(self.num_workers):
            _, msg = self.pipes[idx].recv()
            if msg != "loaded":
                raise RuntimeError(f"Expected msg='loaded', got {msg}")


class MultiSyncDataCollector(_MultiDataCollector):
    """Runs a given number of DataCollectors on separate processes
    synchronously.

    The collection starts when the next item of the collector is queried,
    and no environment step is computed in between the reception of a batch of
    trajectory and the start of the next collection.
    This class can be safely used with online RL algorithms.
    """

    __doc__ += _MultiDataCollector.__doc__

    @property
    def frames_per_batch_worker(self):
        return -(-self.frames_per_batch // self.num_workers)

    @property
    def _queue_len(self) -> int:
        return self.num_workers

    def iterator(self) -> Iterator[_TensorDict]:
        i = -1
        frames = 0
        out_tensordicts_shared = OrderedDict()
        dones = [False for _ in range(self.num_workers)]
        workers_frames = [0 for _ in range(self.num_workers)]
        while not all(dones) and frames < self.total_frames:
            if self.update_at_each_batch:
                self.update_policy_weights_()

            for idx in range(self.num_workers):
                if frames < self.init_random_frames:
                    msg = "continue_random"
                else:
                    msg = "continue"
                self.pipes[idx].send((None, msg))

            i += 1
            max_traj_idx = None
            for k in range(self.num_workers):
                new_data, j = self.queue_out.get()
                if j == 0:
                    data, idx = new_data
                    out_tensordicts_shared[idx] = data
                else:
                    idx = new_data
                workers_frames[idx] = (
                    workers_frames[idx] + out_tensordicts_shared[idx].numel()
                )

                if workers_frames[idx] >= self.total_frames:
                    print(f"{idx} is done!")
                    dones[idx] = True
            for idx in range(self.num_workers):
                traj_ids = out_tensordicts_shared[idx].get("traj_ids")
                if max_traj_idx is not None:
                    traj_ids += max_traj_idx
                    # out_tensordicts_shared[idx].set("traj_ids", traj_ids)
                max_traj_idx = traj_ids.max() + 1
                # out = out_tensordicts_shared[idx]
            out = torch.cat([item for key, item in out_tensordicts_shared.items()], 0)
            if self.split_trajs:
                out = split_trajectories(out)
                frames += out.get("mask").sum()
            else:
                frames += math.prod(out.shape)
            if self.postprocs:
                out = self.postprocs[out.device](out)
            if self._exclude_private_keys:
                excluded_keys = [key for key in out.keys() if key.startswith("_")]
                out = out.exclude(*excluded_keys)
            yield out.clone()

        del out_tensordicts_shared
        self._shutdown_main()


class MultiaSyncDataCollector(_MultiDataCollector):
    """Runs a given number of DataCollectors on separate processes
    asynchronously.

    The collection keeps on occuring on all processes even between the time
    the batch of rollouts is collected and the next call to the iterator.
    This class can be safely used with offline RL algorithms.
    """

    __doc__ += _MultiDataCollector.__doc__

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.out_tensordicts = dict()
        self.running = False

    @property
    def frames_per_batch_worker(self):
        return self.frames_per_batch

    def _get_from_queue(self, timeout=None) -> Tuple[int, int, _TensorDict]:
        new_data, j = self.queue_out.get(timeout=timeout)
        if j == 0:
            data, idx = new_data
            self.out_tensordicts[idx] = data
        else:
            idx = new_data
        out = self.out_tensordicts[idx]
        return idx, j, out

    @property
    def _queue_len(self) -> int:
        return 1

    def iterator(self) -> Iterator[_TensorDict]:
        if self.update_at_each_batch:
            self.update_policy_weights_()

        for i in range(self.num_workers):
            if self.init_random_frames > 0:
                self.pipes[i].send((None, "continue_random"))
            else:
                self.pipes[i].send((None, "continue"))
        self.running = True
        i = -1
        self._frames = 0

        dones = [False for _ in range(self.num_workers)]
        workers_frames = [0 for _ in range(self.num_workers)]
        while self._frames < self.total_frames:
            i += 1
            idx, j, out = self._get_from_queue()

            worker_frames = out.numel()
            if self.split_trajs:
                out = split_trajectories(out)
            self._frames += worker_frames
            workers_frames[idx] = workers_frames[idx] + worker_frames
            if self.postprocs:
                out = self.postprocs[out.device](out)

            # the function blocks here until the next item is asked, hence we send the message to the
            # worker to keep on working in the meantime before the yield statement
            if workers_frames[idx] < self.frames_per_worker:
                if self._frames < self.init_random_frames:
                    msg = "continue_random"
                else:
                    msg = "continue"
                self.pipes[idx].send((idx, msg))
            else:
                print(f"{idx} is done!")
                dones[idx] = True
            if self._exclude_private_keys:
                excluded_keys = [key for key in out.keys() if key.startswith("_")]
                out = out.exclude(*excluded_keys)
            yield out.clone()

        self._shutdown_main()
        self.running = False

    def _shutdown_main(self) -> None:
        if hasattr(self, "out_tensordicts"):
            del self.out_tensordicts
        return super()._shutdown_main()

    def reset(self, reset_idx: Optional[Sequence[bool]] = None) -> None:
        super().reset(reset_idx)
        if self.queue_out.full():
            print("waiting")
            time.sleep(_TIMEOUT)  # wait until queue is empty
        if self.queue_out.full():
            raise Exception("self.queue_out is full")
        if self.running:
            for idx in range(self.num_workers):
                if self._frames < self.init_random_frames:
                    self.pipes[idx].send((idx, "continue_random"))
                else:
                    self.pipes[idx].send((idx, "continue"))


class aSyncDataCollector(MultiaSyncDataCollector):
    """Runs a single DataCollector on a separate process.

    This is mostly useful for offline RL paradigms where the policy being
    trained can differ from the policy used to collect data. In online
    settings, a regular DataCollector should be preferred. This class is
    merely a wrapper around a MultiaSyncDataCollector where a single process
    is being created.

    Args:
        create_env_fn (Callabled): Callable returning an instance of _EnvClass
        policy (Callable, optional): Instance of ProbabilisticTDModule class.
            Must accept _TensorDict object as input.
        total_frames (int): lower bound of the total number of frames returned
            by the collector. In parallel settings, the actual number of
            frames may well be greater than this as the closing signals are
            sent to the workers only once the total number of frames has
            been collected on the server.
        create_env_kwargs (dict, optional): A dictionary with the arguments
            used to create an environment
        max_frames_per_traj: Maximum steps per trajectory. Note that a
            trajectory can span over multiple batches (unless
            reset_at_each_iter is set to True, see below). Once a trajectory
            reaches n_steps_max, the environment is reset. If the
            environment wraps multiple environments together, the number of
            steps is tracked for each environment independently. Negative
            values are allowed, in which case this argument is ignored.
            Default is -1 (i.e. no maximum number of steps)
        frames_per_batch (int): Time-length of a batch.
            reset_at_each_iter and frames_per_batch == n_steps_max are equivalent configurations.
            default: 200
        init_random_frames (int): Number of frames for which the policy is ignored before it is called.
            This feature is mainly intended to be used in offline/model-based settings, where a batch of random
            trajectories can be used to initialize training.
            default=-1 (i.e. no random frames)
        reset_at_each_iter (bool): Whether or not environments should be reset for each batch.
            default=False.
        postproc (callable, optional): A PostProcessor is an object that will read a batch of data and process it in a
            useful format for training.
            default: None.
        split_trajs (bool): Boolean indicating whether the resulting TensorDict should be split according to the trajectories.
            See utils.split_trajectories for more information.
        device (int, str, torch.device, optional): The device on which the
            policy will be placed. If it differs from the input policy
            device, the update_policy_weights_() method should be queried
            at appropriate times during the training loop to accommodate for
            the lag between parameter configuration at various times.
            Default is `None` (i.e. policy is kept on its original device)
        passing_device (int, str, torch.device, optional): The device on which
            the output TensorDict will be stored. For long trajectories,
            it may be necessary to store the data on a different.
            device than the one where the policy is stored. Default is `"cpu"`.
        update_at_each_batch (bool): if True, the policy weights will be updated every time a batch of trajectories
            is collected.
            default=False
        init_with_lag (bool, optional): if True, the first trajectory will be truncated earlier at a random step.
            This is helpful to desynchronize the environments, such that steps do no match in all collected rollouts.
            default = True

    """

    def __init__(
        self,
        create_env_fn: Callable[[], _EnvClass],
        policy: Optional[
            Union[ProbabilisticTDModule, Callable[[_TensorDict], _TensorDict]]
        ] = None,
        total_frames: Optional[int] = -1,
        create_env_kwargs: Optional[dict] = None,
        max_frames_per_traj: int = -1,
        frames_per_batch: int = 200,
        init_random_frames: int = -1,
        reset_at_each_iter: bool = False,
        postproc: Optional[Callable[[_TensorDict], _TensorDict]] = None,
        split_trajs: bool = True,
        device: Optional[Union[int, str, torch.device]] = None,
        passing_device: Union[int, str, torch.device] = "cpu",
        seed: Optional[int] = None,
        pin_memory: bool = False,
    ):
        super().__init__(
            create_env_fn=[create_env_fn],
            policy=policy,
            total_frames=total_frames,
            create_env_kwargs=[create_env_kwargs],
            max_frames_per_traj=max_frames_per_traj,
            frames_per_batch=frames_per_batch,
            reset_at_each_iter=reset_at_each_iter,
            init_random_frames=init_random_frames,
            postproc=postproc,
            split_trajs=split_trajs,
            devices=[device] if device is not None else None,
            passing_devices=[passing_device],
            seed=seed,
            pin_memory=pin_memory,
        )


def _main_async_collector(
    pipe_parent: connection.Connection,
    pipe_child: connection.Connection,
    queue_out: queues.Queue,
    create_env_fn: Union[_EnvClass, "EnvCreator", Callable[[], _EnvClass]],
    create_env_kwargs: dict,
    policy: Callable[[_TensorDict], _TensorDict],
    frames_per_worker: int,
    max_frames_per_traj: int,
    frames_per_batch: int,
    reset_at_each_iter: bool,
    device: Optional[Union[torch.device, str, int]],
    passing_device: Optional[Union[torch.device, str, int]],
    seed: Union[int, Sequence],
    pin_memory: bool,
    idx: int = 0,
    init_with_lag: bool = False,
    exploration_mode: str = "random",
    verbose: bool = False,
) -> None:
    pipe_parent.close()
    #  init variables that will be cleared when closing
    tensordict = data = d = data_in = dc = dc_iter = None

    dc = SyncDataCollector(
        create_env_fn,
        create_env_kwargs=create_env_kwargs,
        policy=policy,
        total_frames=-1,
        max_frames_per_traj=max_frames_per_traj,
        frames_per_batch=frames_per_batch,
        reset_at_each_iter=reset_at_each_iter,
        postproc=None,
        split_trajs=False,
        device=device,
        seed=seed,
        pin_memory=pin_memory,
        passing_device=passing_device,
        return_in_place=True,
        init_with_lag=init_with_lag,
        exploration_mode=exploration_mode,
        return_same_td=True,
    )
    if verbose:
        print("Sync data collector created")
    dc_iter = iter(dc)
    j = 0

    has_timed_out = False
    while True:
        _timeout = _TIMEOUT if not has_timed_out else 1e-3
        if pipe_child.poll(_timeout):
            data_in, msg = pipe_child.recv()
            if verbose:
                print(f"worker {idx} received {msg}")
        else:
            if verbose:
                print(f"poll failed, j={j}")
            # default is "continue" (after first iteration)
            # this is expected to happen if queue_out reached the timeout, but no new msg was waiting in the pipe
            # in that case, the main process probably expects the worker to continue collect data
            if has_timed_out:
                if msg not in ("continue", "continue_random"):
                    raise RuntimeError(f"Unexpected message after time out: msg={msg}")
            else:
                continue
        if msg in ("continue", "continue_random"):
            if msg == "continue_random":
                dc.init_random_frames = float("inf")
            else:
                dc.init_random_frames = -1

            d = next(dc_iter)
            if pipe_child.poll(_MIN_TIMEOUT):
                # in this case, main send a message to the worker while it was busy collecting trajectories.
                # In that case, we skip the collected trajectory and get the message from main. This is faster than
                # sending the trajectory in the queue until timeout when it's never going to be received.
                continue
            if j == 0:
                tensordict = d
                if passing_device is not None and tensordict.device != passing_device:
                    raise RuntimeError(
                        f"expected device to be {passing_device} but got {tensordict.device}"
                    )
                tensordict.share_memory_()
                data = (tensordict, idx)
            else:
                if d is not tensordict:
                    raise RuntimeError(
                        "SyncDataCollector should return the same tensordict modified in-place."
                    )
                data = idx  # flag the worker that has sent its data
            try:
                queue_out.put((data, j), timeout=_TIMEOUT)
                if verbose:
                    print(f"worker {idx} successfully sent data")
                j += 1
                has_timed_out = False
                continue
            except queue.Full:
                if verbose:
                    print(f"worker {idx} has timed out")
                has_timed_out = True
                continue
            # pipe_child.send("done")

        elif msg == "update":
            dc.update_policy_weights_()
            pipe_child.send((j, "updated"))
            has_timed_out = False
            continue

        elif msg == "seed":
            new_seed = dc.set_seed(data_in)
            torch.manual_seed(data_in)
            np.random.seed(data_in)
            pipe_child.send((new_seed, "seeded"))
            has_timed_out = False
            continue

        elif msg == "reset":
            dc.reset()
            pipe_child.send((j, "reset"))
            continue

        elif msg == "state_dict":
            state_dict = dc.state_dict()
            # send state_dict to cpu first
            state_dict = recursive_map_to_cpu(state_dict)
            pipe_child.send((state_dict, "state_dict"))
            has_timed_out = False
            continue

        elif msg == "load_state_dict":
            state_dict = data_in
            dc.load_state_dict(state_dict)
            pipe_child.send((j, "loaded"))
            has_timed_out = False
            continue

        elif msg == "close":
            del tensordict, data, d, data_in
            dc.shutdown()
            del dc, dc_iter
            pipe_child.send("closed")
            if verbose:
                print(f"collector {idx} closed")
            break

        else:
            raise Exception(f"Unrecognized message {msg}")
