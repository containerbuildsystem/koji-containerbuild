#!/bin/bash
set -eux

# Prepare env vars
OS=${OS:="centos"}
OS_VERSION=${OS_VERSION:="7"}
PYTHON_VERSION=${PYTHON_VERSION:="2"}
ACTION=${ACTION:="test"}
IMAGE="$OS:$OS_VERSION"
# Pull fedora images from registry.fedoraproject.org
if [[ $OS == "fedora" ]]; then
  IMAGE="registry.fedoraproject.org/$IMAGE"
fi

CONTAINER_NAME="koji-containerbuild-$OS-$OS_VERSION-py$PYTHON_VERSION"
RUN="docker exec -ti $CONTAINER_NAME"
if [[ $OS == "fedora" ]]; then
  PIP_PKG="python$PYTHON_VERSION-pip"
  PIP="pip$PYTHON_VERSION"
  PKG="dnf"
  PKG_EXTRA="dnf-plugins-core git-core
             python$PYTHON_VERSION-koji python$PYTHON_VERSION-koji-hub
             python$PYTHON_VERSION-jsonschema"
  BUILDDEP="dnf builddep"
  PYTHON="python$PYTHON_VERSION"
else
  PIP_PKG="python-pip"
  PIP="pip"
  PKG="yum"
  PKG_EXTRA="yum-utils git-core koji koji-hub python2-jsonschema"
  BUILDDEP="yum-builddep"
  PYTHON="python"
fi
# Create container if needed
if [[ $(docker ps -q -f name=$CONTAINER_NAME | wc -l) -eq 0 ]]; then
  docker run --name $CONTAINER_NAME -d -v $PWD:$PWD:z -w $PWD -ti $IMAGE sleep infinity
fi

# Install dependencies
if [[ $OS != "fedora" ]]; then $RUN $PKG install -y epel-release; fi
$RUN $PKG install -y $PKG_EXTRA
$RUN $BUILDDEP -y koji-containerbuild.spec
# Install pip package
$RUN $PKG install -y $PIP_PKG
if [[ $PYTHON_VERSION == 3 && $OS_VERSION == rawhide ]]; then
  # https://fedoraproject.org/wiki/Changes/Making_sudo_pip_safe
  $RUN mkdir -p /usr/local/lib/python3.6/site-packages/
fi

# Install other dependencies for tests
if [[ $PYTHON_VERSION == 3 ]]; then
  OSBS_CLIENT_DEPS="python3-PyYAML"
else
  OSBS_CLIENT_DEPS="PyYAML"
fi
$RUN $PKG install -y $OSBS_CLIENT_DEPS

# Install latest osbs-client by installing dependencies from the master branch
# and running pip install with '--no-deps' to avoid compilation
# This would also ensure all the deps are specified in the spec
$RUN rm -rf /tmp/osbs-client
$RUN git clone https://github.com/projectatomic/osbs-client /tmp/osbs-client
$RUN $BUILDDEP -y /tmp/osbs-client/osbs-client.spec
$RUN $PIP install --upgrade --no-deps --force-reinstall git+https://github.com/projectatomic/osbs-client

# Install the latest dockerfile-parse from git
$RUN $PIP install --upgrade --force-reinstall \
    git+https://github.com/containerbuildsystem/dockerfile-parse

# Install koji-containerbuild
$RUN $PYTHON setup.py install

# Install packages for tests
$RUN $PIP install -r tests/requirements.txt

# CentOS needs to have setuptools updates to make pytest-cov work
if [[ $OS != "fedora" ]]; then
  $RUN $PIP install -U setuptools

  # Watch out for https://github.com/pypa/setuptools/issues/937
  $RUN curl -O https://bootstrap.pypa.io/2.6/get-pip.py
  $RUN $PYTHON get-pip.py
fi

case ${ACTION} in
"test")
  TEST_CMD="pytest -vv tests --cov koji_containerbuild"
  ;;
"bandit")
  $RUN $PIP install bandit
  TEST_CMD="bandit-baseline -r koji_containerbuild -ll -ii"
  ;;
*)
  echo "Unknown action: ${ACTION}"
  exit 2
  ;;
esac

# Run tests
$RUN ${TEST_CMD} "$@"

echo "To run tests again:"
echo "$RUN ${TEST_CMD}"
