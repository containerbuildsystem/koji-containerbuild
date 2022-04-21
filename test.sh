#!/bin/bash
set -eux

# Prepare env vars
ENGINE=${ENGINE:="podman"}
OS=${OS:="centos"}
OS_VERSION=${OS_VERSION:="8"}
PYTHON_VERSION=${PYTHON_VERSION:="3"}
ACTION=${ACTION:="test"}
IMAGE="$OS:$OS_VERSION"
CONTAINER_NAME="koji-containerbuild-$OS-$OS_VERSION-py$PYTHON_VERSION"

# Use arrays to prevent globbing and word splitting
engine_mounts=(-v "$PWD":"$PWD":z)
for dir in ${EXTRA_MOUNT:-}; do
  engine_mounts=("${engine_mounts[@]}" -v "$dir":"$dir":z)
done

# Create or resurrect container if needed
if [[ $($ENGINE ps -qa -f name="$CONTAINER_NAME" | wc -l) -eq 0 ]]; then
  $ENGINE run --name "$CONTAINER_NAME" -d "${engine_mounts[@]}" -w "$PWD" -ti "$IMAGE" sleep infinity
elif [[ $($ENGINE ps -q -f name="$CONTAINER_NAME" | wc -l) -eq 0 ]]; then
  echo found stopped existing container, restarting. volume mounts cannot be updated.
  $ENGINE container start "$CONTAINER_NAME"
fi

function setup_kojic() {
  RUN="$ENGINE exec -i $CONTAINER_NAME"
  if [[ $OS == "centos" && $OS_VERSION == "7" ]]; then
    PYTHON="python"
    PIP_PKG="$PYTHON-pip"
    PIP="pip"
    PKG="yum"
    PKG_EXTRA=(yum-utils git-core koji koji-hub)
    BUILDDEP="yum-builddep"
  else
    PYTHON="python$PYTHON_VERSION"
    PIP_PKG="$PYTHON-pip"
    PIP="pip$PYTHON_VERSION"
    PKG="dnf"
    PKG_EXTRA=(dnf-plugins-core git-core "$PYTHON"-koji "$PYTHON"-koji-hub)
    BUILDDEP=(dnf builddep)
  fi

  PIP_INST=("$PIP" install --index-url "${PYPI_INDEX:-https://pypi.org/simple}")

  if [[ $OS == "centos" ]]; then
    $RUN $PKG install -y epel-release;
  fi

  $RUN $PKG install -y "${PKG_EXTRA[@]}"
  [[ ${PYTHON_VERSION} == '3' ]] && WITH_PY3=1 || WITH_PY3=0
  $RUN "${BUILDDEP[@]}" -y koji-containerbuild.spec

  # Install pip package
  $RUN $PKG install -y $PIP_PKG
  if [[ ${WITH_PY3} && $OS_VERSION == rawhide ]]; then
    # https://fedoraproject.org/wiki/Changes/Making_sudo_pip_safe
    $RUN mkdir -p /usr/local/lib/python3.6/site-packages/
  fi

  if [[ ${WITH_PY3} == 1 ]]; then
    # PY3 installing all dependencies

    # Install other dependencies for unit tests
    OSBS_CLIENT_DEPS="python3-PyYAML"

    $RUN $PKG install -y $OSBS_CLIENT_DEPS

    # Install osbs-client dependencies based on specfile
    # from specified git source (default: upstream master)
    $RUN rm -rf /tmp/osbs-client
    $RUN git clone --depth 1 --single-branch \
         https://github.com/projectatomic/osbs-client --branch master /tmp/osbs-client
    # RPM install build dependencies for osbs-client
    $RUN "${BUILDDEP[@]}" --define "with_python3 ${WITH_PY3}" -y /tmp/osbs-client/osbs-client.spec

    # Run pip install with '--no-deps' to avoid compilation.
    # This will also ensure all the deps are specified in the spec
    # Pip install osbs-client from git master
    $RUN "${PIP_INST[@]}" --upgrade --no-deps --force-reinstall \
        git+https://github.com/projectatomic/osbs-client
    # Pip install dockerfile-parse from git master
    $RUN "${PIP_INST[@]}" --upgrade --force-reinstall \
        git+https://github.com/containerbuildsystem/dockerfile-parse

  else
    # PY2 only CLI tests

    if [[ ${OS} == "centos" ]]; then
      # there is no package that could provide more-itertools module on centos7
      # latest version with py2 support in PyPI is 5.0.0, never version causes
      # failures with py2
      $RUN "${PIP_INST[@]}" 'more-itertools==5.*'
    fi
  fi

  # CentOS needs to have setuptools updates to make pytest-cov work
  # setuptools will no longer support python2 starting on version 45
  if [[ $OS == "centos" ]]; then
    $RUN "${PIP_INST[@]}" -U 'setuptools<45'

    # Watch out for https://github.com/pypa/setuptools/issues/937
    $RUN curl -O https://bootstrap.pypa.io/pip/2.6/get-pip.py
    $RUN $PYTHON get-pip.py
  fi

  if [[ $PYTHON_VERSION == 2 ]]; then
    # https://github.com/jaraco/zipp/issues/28
    $RUN "${PIP_INST[@]}" zipp==1.0.0

    # configparser no longer supports python 2
    $RUN "${PIP_INST[@]}" configparser==4.0.2

    # pyrsistent >= 0.17 no longer supports python 2
    # pyrsistent is a dependency of jsonschema
    $RUN "${PIP_INST[@]}" 'pyrsistent==0.16.*'

    # setuptools_scm >= 6 no longer supports python 2
    $RUN "${PIP_INST[@]}" 'setuptools_scm<6'
  fi

  # Workaround problems with dependency hell for older Pythons
  $RUN "${PIP_INST[@]}" -U 'importlib_metadata<3;python_version<"3.8"'

  # Setuptools install koji-c from source
  $RUN $PYTHON setup.py install

  # Pip install packages for unit tests
  $RUN "${PIP_INST[@]}" -r tests/requirements.txt

  # workaround for https://github.com/actions/checkout/issues/766
  $RUN git config --global --add safe.directory "$PWD"
}

case ${ACTION} in
"test")
  setup_kojic
  if [[ $PYTHON_VERSION == 2 ]]; then
    # PY2: only CLI is supported
    TEST_PATHS="tests/test_cli_containerbuild.py"
  else
    TEST_PATHS="tests"
  fi
  TEST_CMD="coverage run --source=koji_containerbuild -m pytest ${TEST_PATHS}"
  ;;
"pylint")
  setup_kojic
  # This can run only at fedora because pylint is not packaged in centos
  # use distro pylint to not get too new pylint version
  $RUN $PKG install -y "${PYTHON}-pylint"
  if [[ $PYTHON_VERSION == 2 ]]; then
    # PY2: only CLI is supported
    PACKAGES='koji_containerbuild/plugins/cli_containerbuild.py tests/test_cli_containerbuild.py'
  else
    PACKAGES='koji_containerbuild tests'
  fi
  TEST_CMD="${PYTHON} -m pylint ${PACKAGES}"
  ;;
"bandit")
  setup_kojic
  if [[ $PYTHON_VERSION == 2 ]]; then
    # PY2: only CLI is supported
    $RUN "${PIP_INST[@]}" "bandit<1.6.2"
    BANDIT_PATHS="koji_containerbuild/plugins/cli_containerbuild.py"
  else
    $RUN "${PIP_INST[@]}" "bandit"
    BANDIT_PATHS="-r koji_containerbuild"
  fi
  TEST_CMD="bandit-baseline ${BANDIT_PATHS} -ll -ii"
  ;;
*)
  echo "Unknown action: ${ACTION}"
  exit 2
  ;;
esac

# Run tests
# shellcheck disable=SC2086
$RUN ${TEST_CMD} "$@"

echo "To run tests again:"
echo "$RUN ${TEST_CMD}"
