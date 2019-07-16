"""
Copyright (C) 2019  Red Hat, Inc.

This library is free software; you can redistribute it and/or
modify it under the terms of the GNU Lesser General Public
License as published by the Free Software Foundation; either
version 2.1 of the License, or (at your option) any later version.

This library is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
Lesser General Public License for more details.

You should have received a copy of the GNU Lesser General Public
License along with this library; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA
"""

from flexmock import flexmock
from textwrap import dedent
from collections import namedtuple
import pytest
import jsonschema
import osbs
import os
import os.path
import signal
import koji
from koji_containerbuild.plugins import builder_containerbuild
from osbs.exceptions import OsbsValidationException
try:
    from osbs.exceptions import OsbsOrchestratorNotEnabled
except ImportError:
    from osbs.exceptions import OsbsValidationException as OsbsOrchestratorNotEnabled


USE_DEFAULT_PKG_INFO = object()


def mock_incremental_upload(session, fname, fd, uploadpath, logger=None):
    pass


builder_containerbuild.incremental_upload = mock_incremental_upload


LogEntry = namedtuple('LogEntry', ['platform', 'line'])
logs = [LogEntry(None, 'orchestrator'),
        LogEntry('x86_64', 'Hurray for bacon: \u2017'),
        LogEntry('x86_64', 'line 2')]


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

    @pytest.mark.parametrize('scratch', [False, True])
    def test_osbs(self, scratch):
        cct = builder_containerbuild.BuildContainerTask(id=1,
                                                        method='buildContainer',
                                                        params='params',
                                                        session='session',
                                                        options='options',
                                                        workdir='workdir')
        cct.opts['scratch'] = scratch
        osbs_obj = cct.osbs()

        expected_conf_section = 'scratch' if scratch else 'default'

        assert type(osbs_obj) is osbs.api.OSBS
        assert osbs_obj.os_conf.conf_section == expected_conf_section
        assert osbs_obj.build_conf.conf_section == expected_conf_section

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

    def _check_orchestrator_logs(self, log_entries, logs_dir):
        def check_meta_entry(filename):
            source_file = os.path.join(koji.pathinfo.work(), filename)
            target_file = os.path.join(logs_dir, filename)

            with open(source_file) as s, open(target_file) as t:
                assert s.read() == t.read()

        log_contents = {}
        for entry in log_entries:
            platform = entry.platform or 'orchestrator'

            if platform == builder_containerbuild.METADATA_TAG:
                check_meta_entry(entry.line)
            else:
                log_contents[platform] = '{old}{new}\n'.format(
                    old=log_contents.get(platform, ''),
                    new=entry.line
                )

        for platform, logs_content in log_contents.items():
            logfile_path = os.path.join(logs_dir, platform + '.log')
            with open(logfile_path) as log_file:
                assert log_file.read() == logs_content

    def _check_non_orchestrator_logs(self, log_entries, logs_dir):
        logs_content = ''.join(line + '\n' for line in log_entries)
        logfile_path = os.path.join(logs_dir, 'openshift-incremental.log')
        with open(logfile_path) as log_file:
            assert log_file.read() == logs_content

    def _check_logfiles(self, log_entries, logs_dir, orchestrator):
        # check that all the log entries for a build are where they are supposed to be
        if orchestrator:
            self._check_orchestrator_logs(log_entries, logs_dir)
        else:
            self._check_non_orchestrator_logs(log_entries, logs_dir)

    @pytest.mark.parametrize('orchestrator', [False, True])
    @pytest.mark.parametrize('get_logs_exc', [None, Exception('error')])
    @pytest.mark.parametrize('build_not_finished', [False, True])
    def test_write_logs(self, tmpdir, orchestrator, get_logs_exc, build_not_finished):
        cct = builder_containerbuild.BuildContainerTask(id=1,
                                                        method='buildContainer',
                                                        params='params',
                                                        session='session',
                                                        options='options',
                                                        workdir='workdir',
                                                        demux=orchestrator)
        if orchestrator:
            get_logs_fname = 'get_orchestrator_build_logs'
            log_entries = [LogEntry(None, 'line 1'),
                           LogEntry(None, 'line 2'),
                           LogEntry('x86_64', 'line 1'),
                           LogEntry('x86_64', 'line 2'),
                           LogEntry(builder_containerbuild.METADATA_TAG, 'x.log')]

            koji_tmpdir = tmpdir.mkdir('koji')
            koji_tmpdir.join('x.log').write('line 1\n'
                                            'line 2\n')

            (flexmock(koji.pathinfo)
                .should_receive('work')
                .and_return(str(koji_tmpdir)))
        else:
            get_logs_fname = 'get_build_logs'
            log_entries = ['line 1',
                           'line 2']

        build_response = flexmock(status=42)
        (build_response
            .should_receive('is_running')
            .and_return(build_not_finished))
        (build_response
            .should_receive('is_pending')
            .and_return(build_not_finished))

        cct._osbs = flexmock()
        (cct._osbs
            .should_receive('get_build')
            .and_return(build_response))

        should_receive = cct._osbs.should_receive(get_logs_fname)
        if get_logs_exc:
            should_receive.and_raise(get_logs_exc)
        else:
            should_receive.and_return(log_entries)

        if get_logs_exc:
            exc_type = builder_containerbuild.ContainerError
            exc_msg = 'Exception while waiting for {what}: {why}'.format(
                what='orchestrator build logs' if orchestrator else 'build logs',
                why=get_logs_exc
            )
        elif build_not_finished:
            exc_type = builder_containerbuild.ContainerError
            exc_msg = ('Build log finished but build still has not finished: {}.'
                       .format(build_response.status))
        else:
            exc_type = exc_msg = None

        if exc_type is not None:
            with pytest.raises(exc_type) as exc_info:
                cct._write_incremental_logs('id', str(tmpdir))
            assert str(exc_info.value) == exc_msg
        else:
            cct._write_incremental_logs('id', str(tmpdir))

        if get_logs_exc is None:
            self._check_logfiles(log_entries, str(tmpdir), orchestrator)

    def _mock_session(self, last_event_id, koji_task_id, pkg_info=USE_DEFAULT_PKG_INFO):
        if pkg_info == USE_DEFAULT_PKG_INFO:
            pkg_info = {'blocked': False}
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
            .with_args(koji_task_id)
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

    def _mock_osbs(self, koji_build_id, src, koji_task_id,
                   orchestrator=False, flatpak=False, build_not_started=False,
                   create_build_args=None):

        create_build_args = create_build_args or {}
        create_build_args.setdefault('git_uri', src['git_uri'])
        create_build_args.setdefault('git_ref', src['git_ref'])
        create_build_args.setdefault('user', 'owner-name')
        create_build_args.setdefault('component', 'fedora-docker')
        create_build_args.setdefault('target', 'target-name')
        create_build_args.setdefault('yum_repourls', [])
        create_build_args.setdefault('scratch', False)
        create_build_args.setdefault('platforms', ['x86_64'])
        create_build_args.setdefault('architecture', None)
        create_build_args.pop('arch_override', None)

        if not create_build_args.get('isolated'):
            create_build_args.pop('isolated', None)

        if not create_build_args.get('compose_ids'):
            create_build_args.pop('compose_ids', None)

        if not create_build_args.get('signing_intent'):
            create_build_args.pop('signing_intent', None)

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
            .should_receive('is_cancelled')
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
                .with_args(koji_task_id=koji_task_id, **create_build_args)
                .times(0 if build_not_started else 1)
                .and_return(build_response))
        else:
            (osbs
                .should_receive('create_orchestrator_build')
                .with_args(koji_task_id=koji_task_id, **create_build_args)
                .times(0 if build_not_started else 1)
                .and_raise(OsbsOrchestratorNotEnabled))

            legacy_args = create_build_args.copy()
            legacy_args.pop('platforms', None)
            legacy_args.pop('koji_parent_build', None)
            legacy_args.pop('isolated', None)
            legacy_args.pop('release', None)
            legacy_args.pop('compose_ids', None)
            legacy_args.pop('signing_intent', None)
            legacy_args['architecture'] = 'x86_64'
            (osbs
                .should_receive('create_build')
                .with_args(koji_task_id=koji_task_id, **legacy_args)
                .times(0 if build_not_started else 1)
                .and_return(build_response))
        (osbs
            .should_receive('wait_for_build_to_get_scheduled')
            .with_args('os-build-id'))
        (osbs.should_receive('cancel_build').never)
        if orchestrator:
            (osbs
                .should_receive('get_orchestrator_build_logs')
                .with_args(build_id='os-build-id', follow=True)
                .and_return(logs))
        else:
            (osbs
                .should_receive('get_build_logs')
                .with_args(build_id='os-build-id', follow=True)
                .and_return(logs))
        (osbs
            .should_receive('wait_for_build_to_finish')
            .with_args('os-build-id')
            .and_return(build_finished_response))

        return osbs

    def _mock_folders(self, tmpdir, dockerfile_content=None, additional_tags_content=None):
        if dockerfile_content is None:
            dockerfile_content = dedent("""\
                FROM fedora

                LABEL com.redhat.component=fedora-docker
                LABEL version=25
                """)

        source_dir = os.path.join(tmpdir, 'sources')
        dockerfile_path = os.path.join(source_dir, 'Dockerfile')
        os.mkdir(source_dir)
        with open(dockerfile_path, 'w') as f:
            f.write(dockerfile_content)

        additional_tags_path = os.path.join(source_dir, 'additional-tags')
        if additional_tags_content is not None:
            with open(additional_tags_path, 'w') as f:
                f.write(additional_tags_content)

        work_dir = os.path.join(tmpdir, 'work_dir')
        os.mkdir(work_dir)

        log_dir = os.path.join(work_dir, 'osbslogs')
        os.mkdir(log_dir)

        return {'dockerfile_path': dockerfile_path}

    def _mock_git_source(self):
        git_uri = 'git://pkgs.example.com/rpms/fedora-docker'
        git_ref = 'b8120b486367ec33fbbfa408542eec7eded8b54e'
        src = git_uri + '#' + git_ref
        return {'git_uri': git_uri, 'git_ref': git_ref, 'src': src}

    def test_checkLabels_missing_labels(self, tmpdir):
        cct = builder_containerbuild.BuildContainerTask(id=1,
                                                        method='buildContainer',
                                                        params='params',
                                                        session='session',
                                                        options='options',
                                                        workdir='workdir')

        dockerfile_content = 'FROM fedora\n'
        missing_labels = ['com.redhat.component (or BZComponent)',
                          'version (or Version)']
        folder_info = self._mock_folders(str(tmpdir),
                                         dockerfile_content=dockerfile_content)
        (flexmock(cct)
            .should_receive('fetchDockerfile')
            .and_return(folder_info['dockerfile_path']))

        with pytest.raises(koji.BuildError) as exc_info:
            cct.checkLabels('src', 'build-tag')

        err_msg = str(exc_info.value)
        assert all(label in err_msg for label in missing_labels)

    @pytest.mark.parametrize(('pkg_info', 'failure'), (
        (None, 'not in list for tag'),
        ({'blocked': True}, 'is blocked for'),
        ({'blocked': False}, None),
    ))
    @pytest.mark.parametrize('orchestrator', (True, False))
    def test_osbs_build(self, tmpdir, pkg_info, failure, orchestrator):
        koji_task_id = 123
        last_event_id = 456
        koji_build_id = 999

        session = self._mock_session(last_event_id, koji_task_id, pkg_info)
        folders_info = self._mock_folders(str(tmpdir))
        src = self._mock_git_source()
        options = flexmock(allowed_scms='pkgs.example.com:/*:no')

        task = builder_containerbuild.BuildContainerTask(id=koji_task_id,
                                                         method='buildContainer',
                                                         params='params',
                                                         session=session,
                                                         options=options,
                                                         workdir='workdir',
                                                         demux=orchestrator)

        (flexmock(task)
            .should_receive('fetchDockerfile')
            .with_args(src['src'], 'build-tag')
            .and_return(folders_info['dockerfile_path']))
        (flexmock(task)
            .should_receive('_write_incremental_logs'))
        if orchestrator:
            (flexmock(task)
                .should_receive('_write_demultiplexed_logs'))
        else:
            (flexmock(task)
                .should_receive('_write_combined_log'))

        task._osbs = self._mock_osbs(koji_build_id=koji_build_id,
                                     src=src,
                                     koji_task_id=koji_task_id,
                                     orchestrator=orchestrator,
                                     build_not_started=bool(failure),
                                     )

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

    @pytest.mark.parametrize('reason, expected_exc_type', [
        ('canceled', builder_containerbuild.ContainerCancelled),
        ('failed', builder_containerbuild.ContainerError),
    ])
    def test_createContainer_failure(self, tmpdir, reason, expected_exc_type):
        koji_task_id = 123
        last_event_id = 456
        koji_build_id = 999

        session = self._mock_session(last_event_id, koji_task_id)
        folders_info = self._mock_folders(str(tmpdir))
        src = self._mock_git_source()
        options = flexmock(allowed_scms='pkgs.example.com:/*:no')

        task = builder_containerbuild.BuildContainerTask(id=koji_task_id,
                                                         method='buildContainer',
                                                         params='params',
                                                         session=session,
                                                         options=options,
                                                         workdir='workdir')

        (flexmock(task)
            .should_receive('fetchDockerfile')
            .with_args(src['src'], 'build-tag')
            .and_return(folders_info['dockerfile_path']))

        task._osbs = self._mock_osbs(koji_build_id=koji_build_id,
                                     src=src,
                                     koji_task_id=koji_task_id)

        build_finished_response = flexmock(status=500, json={})
        (build_finished_response
            .should_receive('is_succeeded')
            .and_return(False))
        (build_finished_response
            .should_receive('is_cancelled')
            .and_return(reason == 'canceled'))
        (build_finished_response
            .should_receive('is_failed')
            .and_return(reason == 'failed'))

        (task._osbs
            .should_receive('wait_for_build_to_finish')
            .and_return(build_finished_response))

        if reason == 'canceled':
            task._osbs.wait_for_build_to_get_scheduled = \
                lambda build_id: os.kill(os.getpid(), signal.SIGINT)
            (task._osbs
                .should_receive('cancel_build')
                .once())
            (session
                .should_receive('cancelTask')
                .once())

        with pytest.raises(expected_exc_type):
            task.handler(src['src'], 'target')

    def test_get_build_target_failed(self, tmpdir):
        koji_task_id = 123
        last_event_id = 456
        session = flexmock()
        (session
            .should_receive('getLastEvent')
            .and_return({'id': last_event_id}))
        (session
            .should_receive('getBuildTarget')
            .with_args('target', event=last_event_id)
            .and_return(None))
        src = self._mock_git_source()
        task = builder_containerbuild.BuildContainerTask(id=koji_task_id,
                                                         method='buildContainer',
                                                         params='params',
                                                         session=session,
                                                         options={},
                                                         workdir=str(tmpdir),
                                                         demux=True)
        with pytest.raises(koji.BuildError) as exc:
            task.handler(src['src'], 'target', opts={})
        assert "Target `target` not found" in str(exc)

    def test_private_branch(self, tmpdir):
        git_uri = 'git://pkgs.example.com/rpms/fedora-docker'
        git_ref = 'private-test1'
        source = git_uri + '#' + git_ref

        koji_task_id = 123
        last_event_id = 456

        options = flexmock(allowed_scms='pkgs.example.com:/*:no')
        folders_info = self._mock_folders(str(tmpdir))

        pkg_info = {'blocked': False}
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
            .with_args(koji_task_id, request=True)
            .and_return({'owner': 'owner'}))
        (session
            .should_receive('getUser')
            .with_args('owner')
            .and_return({'name': 'owner-name'}))
        (session
            .should_receive('getPackageConfig')
            .with_args('dest-tag', 'fedora-docker')
            .and_return(pkg_info))

        task = builder_containerbuild.BuildContainerTask(id=koji_task_id,
                                                         method='buildContainer',
                                                         params='params',
                                                         session=session,
                                                         options=options,
                                                         workdir=str(tmpdir),
                                                         demux=True)
        (flexmock(task)
            .should_receive('getUploadDir')
            .and_return(str(tmpdir)))
        (flexmock(task)
            .should_receive('run_callbacks')
            .times(2))

        (flexmock(koji.daemon.SCM)
            .should_receive('checkout')
            .and_return(folders_info['dockerfile_path']))
        (flexmock(os.path)
            .should_receive('exists')
            .and_return(True))
        task.fetchDockerfile(source, 'build_tag')

    @pytest.mark.parametrize('orchestrator', (True, False))
    @pytest.mark.parametrize('additional_args', (
        {'koji_parent_build': 'fedora-26-99'},
        {'scratch': True},
        {'scratch': False},
        {'isolated': True},
        {'isolated': False},
        {'release': '13'},
    ))
    def test_additional_args(self, tmpdir, orchestrator, additional_args):
        koji_task_id = 123
        last_event_id = 456
        koji_build_id = 999

        session = self._mock_session(last_event_id, koji_task_id)
        folders_info = self._mock_folders(str(tmpdir))
        src = self._mock_git_source()
        options = flexmock(allowed_scms='pkgs.example.com:/*:no')

        task = builder_containerbuild.BuildContainerTask(id=koji_task_id,
                                                         method='buildContainer',
                                                         params='params',
                                                         session=session,
                                                         options=options,
                                                         workdir='workdir',
                                                         demux=orchestrator)

        (flexmock(task)
            .should_receive('fetchDockerfile')
            .with_args(src['src'], 'build-tag')
            .and_return(folders_info['dockerfile_path']))
        (flexmock(task)
            .should_receive('_write_incremental_logs'))
        if orchestrator:
            (flexmock(task)
                .should_receive('_write_demultiplexed_logs'))
        else:
            (flexmock(task)
                .should_receive('_write_combined_log'))

        task._osbs = self._mock_osbs(koji_build_id=koji_build_id,
                                     src=src,
                                     koji_task_id=koji_task_id,
                                     orchestrator=orchestrator,
                                     create_build_args=additional_args.copy())

        task_response = task.handler(src['src'], 'target', opts=additional_args)

        assert task_response == {
            'repositories': ['unique-repo', 'primary-repo'],
            'koji_builds': [koji_build_id]
        }

    def test_flatpak_build(self, tmpdir):
        task_id = 123
        last_event_id = 456
        koji_build_id = 999

        session = self._mock_session(last_event_id, task_id, { 'blocked': False })
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
            .with_args(src['src'], 'build-tag')
            .and_return(folders_info['dockerfile_path']))
        (flexmock(task)
            .should_receive('_write_incremental_logs'))

        additional_args = {
            'flatpak': True,
        }

        task._osbs = self._mock_osbs(koji_build_id=koji_build_id,
                                     src=src,
                                     koji_task_id=task_id,
                                     orchestrator=True, flatpak=True,
                                     create_build_args=additional_args.copy(),
                                     build_not_started=False)
        build_response = flexmock()

        task_response = task.handler(src['src'], 'target', opts={
            'flatpak': True,
        })
        assert task_response == {
            'repositories': ['unique-repo', 'primary-repo'],
            'koji_builds': [koji_build_id]
        }


    @pytest.mark.parametrize('orchestrator', (True, False))
    @pytest.mark.parametrize(('tag', 'release', 'is_oversized'), (
        ('t', None, False),
        ('t'*128, None, False),
        ('t'*129, None, True),
        (None, '1', False),
        (None, '1'*125, False),  # Assumes '25-' prefix for {version}-{release} tag
        (None, '1'*126, True),  # Assumes '25-' prefix for {version}-{release} tag
    ))
    def test_oversized_tags(self, tmpdir, orchestrator, tag, release, is_oversized):
        koji_task_id = 123
        last_event_id = 456
        koji_build_id = 999

        session = self._mock_session(last_event_id, koji_task_id)
        folders_info = self._mock_folders(str(tmpdir), additional_tags_content=tag)
        src = self._mock_git_source()
        options = flexmock(allowed_scms='pkgs.example.com:/*:no')

        task = builder_containerbuild.BuildContainerTask(id=koji_task_id,
                                                         method='buildContainer',
                                                         params='params',
                                                         session=session,
                                                         options=options,
                                                         workdir='workdir',
                                                         demux=orchestrator)

        (flexmock(task)
            .should_receive('fetchDockerfile')
            .with_args(src['src'], 'build-tag')
            .and_return(folders_info['dockerfile_path']))
        (flexmock(task)
            .should_receive('_write_incremental_logs'))
        if orchestrator:
            (flexmock(task)
                .should_receive('_write_demultiplexed_logs'))
        else:
            (flexmock(task)
                .should_receive('_write_combined_log'))

        additional_args = {}
        if release:
            additional_args['release'] = release
        task._osbs = self._mock_osbs(koji_build_id=koji_build_id,
                                     src=src,
                                     koji_task_id=koji_task_id,
                                     orchestrator=orchestrator,
                                     create_build_args=additional_args,
                                     build_not_started=is_oversized)

        if is_oversized:
            with pytest.raises(koji.BuildError) as exc_info:
                task.handler(src['src'], 'target', opts=additional_args)

            assert 'cannot create image with a tag longer than 128' in str(exc_info.value)
        else:
            task_response = task.handler(src['src'], 'target', opts=additional_args)

            assert task_response == {
                'repositories': ['unique-repo', 'primary-repo'],
                'koji_builds': [koji_build_id]
            }

    @pytest.mark.parametrize('orchestrator', (True, False))
    @pytest.mark.parametrize(('df_release', 'param_release', 'expected'), (
        ('10', '11', '11'),
        (None, '11', '11'),
        ('10', None, '10'),
    ))
    def test_build_nvr_exists(self, tmpdir, orchestrator, df_release, param_release, expected):
        koji_task_id = 123
        last_event_id = 456
        koji_build_id = 999

        session = self._mock_session(last_event_id, koji_task_id)

        (session
            .should_receive('getBuild')
            .with_args('fedora-docker-25-%s' % expected)
            .and_return({'id': last_event_id}))

        dockerfile_content = dedent("""\
            FROM fedora

            LABEL com.redhat.component=fedora-docker
            LABEL version=25
            """)
        if df_release:
            dockerfile_content += 'LABEL release=%s' % df_release

        folders_info = self._mock_folders(str(tmpdir), dockerfile_content=dockerfile_content)
        src = self._mock_git_source()
        options = flexmock(allowed_scms='pkgs.example.com:/*:no')

        task = builder_containerbuild.BuildContainerTask(id=koji_task_id,
                                                         method='buildContainer',
                                                         params='params',
                                                         session=session,
                                                         options=options,
                                                         workdir='workdir',
                                                         demux=orchestrator)

        (flexmock(task)
            .should_receive('fetchDockerfile')
            .with_args(src['src'], 'build-tag')
            .and_return(folders_info['dockerfile_path']))
        (flexmock(task)
            .should_receive('_write_incremental_logs'))
        if orchestrator:
            (flexmock(task)
                .should_receive('_write_demultiplexed_logs'))
        else:
            (flexmock(task)
                .should_receive('_write_combined_log'))

        additional_args = {}
        if param_release:
            additional_args['release'] = param_release
        task._osbs = self._mock_osbs(koji_build_id=koji_build_id,
                                     src=src,
                                     koji_task_id=koji_task_id,
                                     orchestrator=orchestrator,
                                     create_build_args=additional_args,
                                     build_not_started=True)

        with pytest.raises(koji.BuildError) as exc_info:
            task.handler(src['src'], 'target', opts=additional_args)
        assert 'already exists' in str(exc_info.value)

    @pytest.mark.parametrize(('additional_args', 'raises'), (
        ({}, False),
        ({'compose_ids': [1, 2]}, False),
        ({'signing_intent': 'intent1'}, False),
        ({'compose_ids': [1, 2], 'signing_intent': 'intent1'}, True),
        ({'compose_ids': [1, 2], 'yum_repourls': ['www.repo.com']}, True)
    ))
    def test_compose_ids_and_signing_intent(self, tmpdir, additional_args, raises):
        koji_task_id = 123
        last_event_id = 456
        koji_build_id = 999

        session = self._mock_session(last_event_id, koji_task_id)
        folders_info = self._mock_folders(str(tmpdir))
        src = self._mock_git_source()
        options = flexmock(allowed_scms='pkgs.example.com:/*:no')

        task = builder_containerbuild.BuildContainerTask(id=koji_task_id,
                                                         method='buildContainer',
                                                         params='params',
                                                         session=session,
                                                         options=options,
                                                         workdir='workdir',
                                                         demux=True)

        (flexmock(task)
            .should_receive('fetchDockerfile')
            .with_args(src['src'], 'build-tag')
            .and_return(folders_info['dockerfile_path']))
        (flexmock(task)
            .should_receive('_write_incremental_logs'))
        (flexmock(task)
            .should_receive('_write_demultiplexed_logs'))

        task._osbs = self._mock_osbs(koji_build_id=koji_build_id,
                                     src=src,
                                     koji_task_id=koji_task_id,
                                     orchestrator=True,
                                     build_not_started=raises,
                                     create_build_args=additional_args.copy())

        if raises:
            with pytest.raises(koji.BuildError):
                task.handler(src['src'], 'target', opts=additional_args)
        else:
            task_response = task.handler(src['src'], 'target', opts=additional_args)

            assert task_response == {
                'repositories': ['unique-repo', 'primary-repo'],
                'koji_builds': [koji_build_id]
            }

    @pytest.mark.parametrize('orchestrator', (True, False))
    @pytest.mark.parametrize(('additional_args', 'raises'), (
        ({'scratch': True, 'arch_override': 'x86_64'}, False),
        ({'scratch': True, 'arch_override': ''}, False),
        ({'scratch': False, 'arch_override': 'x86_64'}, True),
        ({'scratch': False, 'arch_override': ''}, False),
        ({'isolated': True, 'arch_override': 'x86_64'}, False),
        ({'isolated': True, 'arch_override': ''}, False),
        ({'isolated': False, 'arch_override': 'x86_64'}, True),
        ({'isolated': False, 'arch_override': ''}, False),
        ({'scratch': True, 'isolated': True, 'arch_override': 'x86_64'}, False),
        ({'scratch': True, 'isolated': True, 'arch_override': ''}, False),
        ({'scratch': False, 'isolated': True, 'arch_override': 'x86_64'}, False),
        ({'scratch': False, 'isolated': True, 'arch_override': ''}, False),
        ({'scratch': True, 'isolated': False, 'arch_override': 'x86_64'}, False),
        ({'scratch': True, 'isolated': False, 'arch_override': ''}, False),
        ({'scratch': False, 'isolated': False, 'arch_override': 'x86_64'}, True),
        ({'scratch': False, 'isolated': False, 'arch_override': ''}, False),
    ))
    def test_arch_override(self, tmpdir, orchestrator, additional_args, raises):
        koji_task_id = 123
        last_event_id = 456
        koji_build_id = 999

        session = self._mock_session(last_event_id, koji_task_id)
        folders_info = self._mock_folders(str(tmpdir))
        src = self._mock_git_source()
        options = flexmock(allowed_scms='pkgs.example.com:/*:no')

        task = builder_containerbuild.BuildContainerTask(id=koji_task_id,
                                                         method='buildContainer',
                                                         params='params',
                                                         session=session,
                                                         options=options,
                                                         workdir='workdir',
                                                         demux=orchestrator)

        (flexmock(task)
            .should_receive('fetchDockerfile')
            .with_args(src['src'], 'build-tag')
            .and_return(folders_info['dockerfile_path']))
        (flexmock(task)
            .should_receive('_write_incremental_logs'))
        if orchestrator:
            (flexmock(task)
                .should_receive('_write_demultiplexed_logs'))
        else:
            (flexmock(task)
                .should_receive('_write_combined_log'))

        task._osbs = self._mock_osbs(koji_build_id=koji_build_id,
                                     src=src,
                                     koji_task_id=koji_task_id,
                                     orchestrator=orchestrator,
                                     build_not_started=raises,
                                     create_build_args=additional_args.copy())

        if raises:
            with pytest.raises(koji.BuildError):
                task.handler(src['src'], 'target', opts=additional_args)
        else:
            task_response = task.handler(src['src'], 'target', opts=additional_args)

            assert task_response == {
                'repositories': ['unique-repo', 'primary-repo'],
                'koji_builds': [koji_build_id]
            }

    @pytest.mark.parametrize('arg_name, arg_value, expected_types', [
        ('src', None, [u'string']),
        ('src', 123, [u'string']),

        ('target', None, [u'string']),
        ('target', 123, [u'string']),

        ('opts', '{"a": "b"}', [u'object', u'null']),
        ('opts', 123, [u'object', u'null']),
        ('opts', [], [u'object', u'null']),
    ])
    def test_schema_validation_invalid_arg_types(self, arg_name, arg_value, expected_types):
        task = builder_containerbuild.BuildContainerTask(id=1,
                                                         method='buildContainer',
                                                         params={},
                                                         session=None,
                                                         options=None,
                                                         workdir='not used')

        task_args = {
            'src': arg_value if arg_name == 'src' else 'abc',
            'target': arg_value if arg_name == 'target' else 'xyz',
            'opts': arg_value if arg_name == 'opts' else None,
        }

        with pytest.raises(jsonschema.ValidationError) as exc_info:
            task.handler(**task_args)

        expected_types_str = ', '.join('{!r}'.format(t) for t in expected_types)
        err_msg = 'is not of type {}'.format(expected_types_str)
        assert err_msg in str(exc_info.value)

    @pytest.mark.parametrize('property_name, property_value, expected_types', [
        ('scratch', 'true', [u'boolean']),
        ('scratch', 1, [u'boolean']),
        ('scratch', None, [u'boolean']),

        ('isolated', 'true', [u'boolean']),
        ('isolated', 1, [u'boolean']),
        ('isolated', None, [u'boolean']),

        ('yum_repourls', 'just.one.url', [u'array', u'null']),
        ('yum_repourls', ['some.url', 1], [u'string']),

        ('git_branch', 123, [u'string', u'null']),
        ('push_url', 123, [u'string', u'null']),
        ('koji_parent_build', 123, [u'string', u'null']),
        ('release', 123, [u'string', u'null']),

        ('flatpak', 'true', [u'boolean']),
        ('flatpak', 1, [u'boolean']),
        ('flatpak', None, [u'boolean']),

        ('compose_ids', 1, [u'array', u'null']),
        ('compose_ids', [1, '2'], [u'integer']),
        ('compose_ids', [1.5], [u'integer']),

        ('signing_intent', 123, [u'string', u'null']),
    ])
    def test_schema_validation_invalid_type_for_opts_property(self,
                                                              property_name,
                                                              property_value,
                                                              expected_types):

        task = builder_containerbuild.BuildContainerTask(id=1,
                                                         method='buildContainer',
                                                         params={},
                                                         session=None,
                                                         options=None,
                                                         workdir='not used')

        build_opts = {property_name: property_value}

        with pytest.raises(jsonschema.ValidationError) as exc_info:
            task.handler('source', 'target', build_opts)

        expected_types_str = ', '.join('{!r}'.format(t) for t in expected_types)
        err_msg = 'is not of type {}'.format(expected_types_str)
        assert err_msg in str(exc_info.value)

    @pytest.mark.parametrize('build_opts', [
        None,
        {},
        {'unknown_property': 'validates (for now)'},

        {'scratch': False,
         'isolated': False,
         'yum_repourls': None,
         'git_branch': None,
         'push_url': None,
         'koji_parent_build': None,
         'release': None,
         'flatpak': False,
         'compose_ids': None,
         'signing_intent': None},

        {'scratch': False,
         'isolated': False,
         'yum_repourls': ['url.1', 'url.2'],
         'git_branch': 'master',
         'push_url': 'here.please',
         'koji_parent_build': 'some-or-other',
         'release': 'v8',
         'flatpak': False,
         'compose_ids': [1, 2, 3],
         'signing_intent': 'No, I do not intend to sign anything.'},
    ])
    def test_schema_validation_valid_options(self, build_opts, tmpdir):
        koji_task_id = 123
        last_event_id = 456

        session = self._mock_session(last_event_id, koji_task_id)
        folders_info = self._mock_folders(str(tmpdir))
        src = self._mock_git_source()

        task = builder_containerbuild.BuildContainerTask(id=koji_task_id,
                                                         method='buildContainer',
                                                         params={},
                                                         session=session,
                                                         options={},
                                                         workdir=str(tmpdir))

        (flexmock(task)
            .should_receive('fetchDockerfile')
            .with_args(src['src'], 'build-tag')
            .and_return(folders_info['dockerfile_path']))

        (flexmock(task)
            .should_receive('runBuilds')
            .and_return([{'repository': 'something.somewhere'}]))

        task.handler(src['src'], 'target', build_opts)
