#!/usr/bin/env bash

unset PYTORCH_VERSION
# For unittest, nightly PyTorch is used as the following section,
# so no need to set PYTORCH_VERSION.
# In fact, keeping PYTORCH_VERSION forces us to hardcode PyTorch version in config.

set -e

eval "$(./conda/bin/conda shell.bash hook)"
conda activate ./env

if [ "${CU_VERSION:-}" == cpu ] ; then
    cudatoolkit="cpuonly"
    version="cpu"
else
    if [[ ${#CU_VERSION} -eq 4 ]]; then
        CUDA_VERSION="${CU_VERSION:2:1}.${CU_VERSION:3:1}"
    elif [[ ${#CU_VERSION} -eq 5 ]]; then
        CUDA_VERSION="${CU_VERSION:2:2}.${CU_VERSION:4:1}"
    fi
    echo "Using CUDA $CUDA_VERSION as determined by CU_VERSION ($CU_VERSION)"
    version="$(python -c "print('.'.join(\"${CUDA_VERSION}\".split('.')[:2]))")"
    cudatoolkit="cudatoolkit=${version}"
fi

case "$(uname -s)" in
    Darwin*) os=MacOSX;;
    *) os=Linux
esac

# submodules
git submodule sync && git submodule update --init --recursive

#printf "Installing PyTorch with %s\n" "${cudatoolkit}"
#if [ "${os}" == "MacOSX" ]; then
#    conda install -y -c "pytorch-${UPLOAD_CHANNEL}" "pytorch-${UPLOAD_CHANNEL}"::pytorch "${cudatoolkit}" pytest
#else
#    conda install -y -c "pytorch-${UPLOAD_CHANNEL}" "pytorch-${UPLOAD_CHANNEL}"::pytorch[build="*${version}*"] "${cudatoolkit}" pytest
#fi

#printf "Installing PyTorch with %s\n" "${CU_VERSION}"
if [ "${CU_VERSION:-}" == cpu ] ; then
    # use pip to install pytorch as conda can frequently pick older release
    pip install torch -f https://download.pytorch.org/whl/nightly/cpu/torch_nightly.html --pre
else
    conda install -y pytorch cudatoolkit=10.2 -c pytorch-nightly
fi

printf "Installing functorch\n"
pip install ninja  # Makes the build go faster
pip install "git+https://github.com/pytorch/functorch.git"

# smoke test
python -c "import functorch"

printf "* Installing torchrl\n"
python setup.py develop

# smoke test
python -c "import torchrl"
