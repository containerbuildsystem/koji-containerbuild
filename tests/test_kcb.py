"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.
This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import sys
from flexmock import flexmock
import pytest
import osbs
from koji_containerbuild.plugins import builder_containerbuild


class KojidMock(object):
    """Mock the kojid module"""
    def incremental_upload(self, session, fname, fd, uploadpath, logger=None):
        pass


builder_containerbuild.kojid = KojidMock()


class TestBuilder(object):
    @pytest.mark.parametrize("resdir", ['test', 'test2'])
    def test_resultdir(self, resdir):
        cct = builder_containerbuild.CreateContainerTask(id=1, method='createContainer', params='params', session='session', options='options', workdir=resdir)
        assert cct.resultdir() == '%s/osbslogs' % resdir

    def test_osbs(self):
        cct = builder_containerbuild.CreateContainerTask(id=1, method='createContainer', params='params', session='session', options='options', workdir='workdir')
        assert type(cct.osbs()) is osbs.api.OSBS

    @pytest.mark.parametrize("repos", [{'repo1': 'test1'}, {'repo2': 'test2'}])
    def test_get_repositories(self, repos):
        response = flexmock(get_repositories=lambda: repos)
        cct = builder_containerbuild.CreateContainerTask(id=1, method='createContainer', params='params', session='session', options='options', workdir='workdir')
        repositories = [] 
        for repo in repos.values():
            repositories.extend(repo)
        assert set(cct._get_repositories(response)) ^ set(repositories) == set([])
