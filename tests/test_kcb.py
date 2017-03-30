"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.
This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from flexmock import flexmock
from textwrap import dedent
import pytest
import osbs
import os
import os.path
import koji
from koji_containerbuild.plugins import builder_containerbuild
from osbs.exceptions import OsbsValidationException


class KojidMock(object):
    """Mock the kojid module"""
    def incremental_upload(self, session, fname, fd, uploadpath, logger=None):
        pass


builder_containerbuild.kojid = KojidMock()


class TestBuilder(object):
    @pytest.mark.parametrize("resdir", ['test', 'test2'])
    def test_resultdir(self, resdir):
        cct = builder_containerbuild.BuildContainerTask(id=1,
                                                        method='buildContainer',
                                                        params='params',
                                                        session='session',
                                                        options='options',
                                                        workdir=resdir)
        assert cct.resultdir() == '%s/osbslogs' % resdir

    def test_osbs(self):
        cct = builder_containerbuild.BuildContainerTask(id=1,
                                                        method='buildContainer',
                                                        params='params',
                                                        session='session',
                                                        options='options',
                                                        workdir='workdir')
        assert type(cct.osbs()) is osbs.api.OSBS

    @pytest.mark.parametrize("repos", [{'repo1': 'test1'}, {'repo2': 'test2'}])
    def test_get_repositories(self, repos):
        response = flexmock(get_repositories=lambda: repos)
        cct = builder_containerbuild.BuildContainerTask(id=1,
                                                        method='buildContainer',
                                                        params='params',
                                                        session='session',
                                                        options='options',
                                                        workdir='workdir')
        repositories = []
        for repo in repos.values():
            repositories.extend(repo)
        assert set(cct._get_repositories(response)) ^ set(repositories) == set([])

    def _mock_session(self, last_event_id, task_id, pkg_info):
        session = flexmock()
        (session
            .should_receive('getLastEvent')
            .and_return({'id': last_event_id}))
        (session
            .should_receive('getBuildTarget')
            .with_args('target', event=last_event_id)
            .and_return({'build_tag': 'build-tag', 'name': 'target-name',
                         'dest_tag_name': 'dest-tag'}))
        (session
            .should_receive('getBuildConfig')
            .with_args('build-tag', event=last_event_id)
            .and_return({'arches': 'x86_64'}))
        (session
            .should_receive('getTaskInfo')
            .with_args(task_id)
            .and_return({'owner': 'owner'}))
        (session
            .should_receive('getUser')
            .with_args('owner')
            .and_return({'name': 'owner-name'}))
        (session
            .should_receive('getPackageConfig')
            .with_args('dest-tag', 'fedora-docker')
            .and_return(pkg_info))

        return session

    def _mock_osbs(self, koji_build_id, git_uri, git_ref, task_id,
                   orchestrator=False, build_not_started=False):
        build_response = flexmock()
        (build_response
            .should_receive('get_build_name')
            .and_return('os-build-id'))

        build_finished_response = flexmock(status='200', json={})
        (build_finished_response
            .should_receive('is_succeeded')
            .and_return(True))
        (build_finished_response
            .should_receive('is_failed')
            .and_return(False))
        (build_finished_response
            .should_receive('get_koji_build_id')
            .and_return(koji_build_id))
        (build_finished_response
            .should_receive('get_repositories')
            .and_return({'unique': ['unique-repo'], 'primary': ['primary-repo']}))

        osbs = flexmock()
        if orchestrator:
            (osbs
                .should_receive('create_orchestrator_build')
                .with_args(git_uri=git_uri, git_ref=git_ref, user='owner-name',
                           component='fedora-docker', target='target-name',
                           architecture=None, yum_repourls=[],
                           platforms=['x86_64'], scratch=False, koji_task_id=task_id)
                .times(0 if build_not_started else 1)
                .and_return(build_response))
        else:
            (osbs
                .should_receive('create_orchestrator_build')
                .with_args(git_uri=git_uri, git_ref=git_ref, user='owner-name',
                           component='fedora-docker', target='target-name',
                           architecture=None, yum_repourls=[],
                           platforms=['x86_64'], scratch=False, koji_task_id=task_id)
                .times(0 if build_not_started else 1)
                .and_raise(OsbsValidationException))

            (osbs
                .should_receive('create_build')
                .with_args(git_uri=git_uri, git_ref=git_ref, user='owner-name',
                           component='fedora-docker', target='target-name',
                           architecture='x86_64', yum_repourls=[],
                           scratch=False, koji_task_id=task_id)
                .times(0 if build_not_started else 1)
                .and_return(build_response))
        (osbs
            .should_receive('wait_for_build_to_get_scheduled')
            .with_args('os-build-id'))
        (osbs
            .should_receive('wait_for_build_to_finish')
            .with_args('os-build-id')
            .and_return(build_finished_response))

        return osbs

    def _mock_folders(self, tmpdir):
        source_dir = os.path.join(tmpdir, 'source')
        dockerfile_path = os.path.join(source_dir, 'Dockerfile')
        os.mkdir(source_dir)
        with open(dockerfile_path, 'w') as f:
            f.write(dedent("""\
                FROM fedora

                LABEL com.redhat.component=fedora-docker
                LABEL version=25
                """))

        work_dir = os.path.join(tmpdir, 'work_dir')
        os.mkdir(work_dir)

        return {'dockerfile_path': dockerfile_path}

    def _mock_git_source(self):
        git_uri = 'git://pkgs.example.com/rpms/fedora-docker'
        git_ref = 'b8120b486367ec33fbbfa408542eec7eded8b54e'
        src = git_uri + '#' + git_ref
        return {'git_uri': git_uri, 'git_ref': git_ref, 'src': src}

    @pytest.mark.parametrize(('pkg_info', 'failure'), (
        (None, 'not in list for tag'),
        ({'blocked': True}, 'is blocked for'),
        ({'blocked': False}, None),
    ))
    @pytest.mark.parametrize('orchestrator', (True, False))
    def test_osbs_build(self, tmpdir, pkg_info, failure, orchestrator):
        task_id = 123
        last_event_id = 456
        koji_build_id = 999

        session = self._mock_session(last_event_id, task_id, pkg_info)
        folders_info = self._mock_folders(str(tmpdir))
        src = self._mock_git_source()
        options = flexmock(allowed_scms='pkgs.example.com:/*:no')

        task = builder_containerbuild.BuildContainerTask(id=task_id,
                                                         method='buildContainer',
                                                         params='params',
                                                         session=session,
                                                         options=options,
                                                         workdir='workdir')

        (flexmock(task)
            .should_receive('fetchDockerfile')
            .with_args(src['src'])
            .and_return(folders_info['dockerfile_path']))
        (flexmock(task)
            .should_receive('_write_incremental_logs'))

        task._osbs = self._mock_osbs(koji_build_id=koji_build_id,
                                     git_uri=src['git_uri'],
                                     git_ref=src['git_ref'],
                                     task_id=task_id,
                                     orchestrator=orchestrator,
                                     build_not_started=bool(failure))
        build_response = flexmock()

        if failure:
            with pytest.raises(koji.BuildError) as exc:
                task.handler(src['src'], 'target', opts={})

            assert failure in str(exc)

        else:
            task_response = task.handler(src['src'], 'target', opts={})

            assert task_response == {
                'repositories': ['unique-repo', 'primary-repo'],
                'koji_builds': [koji_build_id]
            }
