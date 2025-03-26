import sys
from setuptools import setup
from setuptools.command.sdist import sdist
from setuptools.command.build_py import build_py
import subprocess


class TitoDist(sdist):
    def run(self):
        subprocess.call(["tito", "build", "--tgz", "-o", "."])


def _get_requirements(requirements_file='requirements.txt'):
    with open(requirements_file) as f:
        return [
            line.split('#')[0].rstrip()
            for line in f.readlines()
            if not line.startswith('#')
            ]


def _install_requirements():
    if sys.version_info[0] >= 3:
        requirements = _get_requirements('requirements.txt')
    else:
        requirements = _get_requirements('requirements-py2.txt')
    return requirements


class Py2CLIOnlyBuild(build_py):

    excluded = {
        'koji_containerbuild/plugins/builder_containerbuild.py',
        'koji_containerbuild/plugins/hub_containerbuild.py',
    }

    def find_package_modules(self, package, package_dir):
        modules = build_py.find_package_modules(self, package, package_dir)
        return [
            (pkg, mod, file, ) for (pkg, mod, file, ) in modules
            if file not in self.excluded
        ]


setup(
    name="koji-containerbuild",
    version="1.4.0",
    author="Pavol Babincak",
    author_email="pbabinca@redhat.com",
    description="Container build support for Koji buildsystem",
    license="LGPLv2+",
    url="https://github.com/containerbuildsystem/koji-containerbuild",
    packages=[
        'koji_containerbuild',
        'koji_containerbuild.plugins',
    ],
    install_requires=_install_requirements(),
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Topic :: Internet",
        "License :: OSI Approved :: GNU Lesser General Public License v2"
        " or later (LGPLv2+)",
    ],
    cmdclass={
        'sdist': TitoDist,
        # Only CLI for Py2
        'build_py': build_py if sys.version_info[0] >= 3 else Py2CLIOnlyBuild,
    }
)
