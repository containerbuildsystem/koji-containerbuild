from setuptools import setup
from setuptools.command.sdist import sdist
import subprocess


class TitoDist(sdist):
    def run(self):
        subprocess.call(["tito", "build", "--tgz", "-o", "."])

setup(
    name="koji-containerbuild",
    version="0.7.5",
    author="Pavol Babincak",
    author_email="pbabinca@redhat.com",
    description="Container build support for Koji buildsystem",
    license="LGPLv2+",
    url="https://github.com/containerbuildsystem/koji-containerbuild",
    packages=[
        'koji_containerbuild',
        'koji_containerbuild.plugins',
    ],
    install_requires=[],
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Topic :: Internet",
        "License :: OSI Approved :: GNU Lesser General Public License v2"
        " or later (LGPLv2+)",
    ],
    cmdclass={
        'sdist': TitoDist,
    },
    package_data={
        'koji_containerbuild': ['schemas/*.json']
    }
)
