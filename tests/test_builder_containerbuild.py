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
from __future__ import absolute_import

from copy import deepcopy
import os
import os.path
import signal
from textwrap import dedent

import json
import jsonschema
import koji
import pytest
from flexmock import flexmock

import osbs
from osbs.exceptions import OsbsValidationException
from osbs.utils import UserWarningsStore

from koji_containerbuild.plugins import builder_containerbuild

USE_DEFAULT_PKG_INFO = object()


def mock_incremental_upload(session, fname, fd, uploadpath, logger=None):
    pass


class mock_time():
    def sleep(self, *args):
        return


builder_containerbuild.incremental_upload = mock_incremental_upload
builder_containerbuild.time = mock_time


logs = ['normal log entry',
        u'Hurray for bacon: \u2017',
        'line 2',
        'another log entry']


class TestBuilder(object):
    @pytest.mark.parametrize(('task_method', 'method'), [
        (builder_containerbuild.BuildContainerTask, 'buildContainer'),
        (builder_containerbuild.BuildSourceContainerTask, 'buildSourceContainer'),
    ])
    @pytest.mark.parametrize("resdir", ['test', 'test2'])
    def test_resultdir(self, task_method, method, resdir):
        cct = task_method(id=1,
                          method=method,
                          params='params',
                          session='session',
                          options='options',
                          workdir=resdir)
        assert cct.resultdir() == '%s/osbslogs' % resdir

    @pytest.mark.parametrize(('task_method', 'method'), [
        (builder_containerbuild.BuildContainerTask, 'buildContainer'),
        (builder_containerbuild.BuildSourceContainerTask, 'buildSourceContainer'),
    ])
    @pytest.mark.parametrize('scratch', [False, True])
    def test_osbs(self, task_method, method, scratch):
        cct = task_method(id=1,
                          method=method,
                          params='params',
                          session='session',
                          options='options',
                          workdir='workdir')
        cct.opts['scratch'] = scratch
        osbs_obj = cct.osbs()

        if method == 'buildContainer':
            expected_conf_section = 'default_binary'
        if method == 'buildSourceContainer':
            expected_conf_section = 'default_source'

        assert isinstance(osbs_obj, osbs.api.OSBS)
        assert osbs_obj.os_conf.conf_section == expected_conf_section

    @pytest.mark.parametrize("repos", [{'repo1': 'test1'}, {'repo2': 'test2'}])
    def test_get_repositories(self, repos):
        cct = builder_containerbuild.BuildContainerTask(id=1,
                                                        method='buildContainer',
                                                        params='params',
                                                        session='session',
                                                        options='options',
                                                        workdir='workdir')
        repositories = []
        for repo in repos.values():
            repositories.extend(repo)

        assert set(cct._get_repositories({'repositories': json.dumps(repos)})) == set(repositories)

    def _check_logfiles(self, log_entries, logs_dir):
        def check_meta_entry(filename):
            source_file = os.path.join(koji.pathinfo.work(), filename)
            target_file = os.path.join(logs_dir, filename)

            with open(source_file) as s, open(target_file) as t:
                assert s.read() == t.read()

        user_warnings = UserWarningsStore()
        log_contents = {}
        log_name = 'osbs_build'
        for line in log_entries:

            if builder_containerbuild.METADATA_TAG in line:
                _, meta_file = line.rsplit(' ', 1)
                check_meta_entry(meta_file)
            elif user_warnings.is_user_warning(line):
                user_warnings.store(line)
            else:
                log_contents[log_name] = '{old}{new}\n'.format(
                    old=log_contents.get(log_name, ''),
                    new=line
                )

        if user_warnings:
            log_contents['user_warnings'] = str(user_warnings)

        for log_name, logs_content in log_contents.items():
            logfile_path = os.path.join(logs_dir, log_name + '.log')
            with open(logfile_path) as log_file:
                assert log_file.read() == logs_content

    @pytest.mark.parametrize('get_logs_exc', [None, Exception('error')])
    @pytest.mark.parametrize('build_not_finished', [False, True])
    def test_write_logs(self, tmpdir, get_logs_exc, build_not_finished):
        cct = builder_containerbuild.BuildContainerTask(id=1,
                                                        method='buildContainer',
                                                        params='params',
                                                        session='session',
                                                        options='options',
                                                        workdir='workdir')

        log_entries = [
            'line 1',
            'line 2',
            'log - USER_WARNING - {"message": "message"}',
            'x86_64 line 1',
            'x86_64 line 2',
            'x86_64 log - USER_WARNING - {"message": "message"}',
            'x86_64 log - USER_WARNING - {"message": "another_message"}',
            builder_containerbuild.METADATA_TAG + ' x.log'
        ]

        koji_tmpdir = tmpdir.mkdir('koji')
        koji_tmpdir.join('x.log').write('line 1\n'
                                        'line 2\n')

        (flexmock(koji.pathinfo)
            .should_receive('work')
            .and_return(str(koji_tmpdir)))

        (flexmock(osbs.api.OSBS)
            .should_receive('build_not_finished').and_return(build_not_finished))
        if build_not_finished:
            (flexmock(osbs.api.OSBS)
                .should_receive('get_build_reason').and_return('Failed'))

        if get_logs_exc:
            (flexmock(osbs.api.OSBS)
                .should_receive('get_build_logs')
                .and_raise(get_logs_exc))
        else:
            (flexmock(osbs.api.OSBS)
                .should_receive('get_build_logs')
                .and_return(log_entries))

        if get_logs_exc:
            exc_type = builder_containerbuild.ContainerError
            exc_msg = f'Exception while waiting for build logs: {get_logs_exc}'
        elif build_not_finished:
            exc_type = builder_containerbuild.ContainerError
            exc_msg = 'Build log finished but build still has not finished: Failed.'
        else:
            exc_type = exc_msg = None

        if exc_type is not None:
            with pytest.raises(exc_type) as exc_info:
                cct._write_incremental_logs('id', str(tmpdir))
            assert str(exc_info.value) == exc_msg
        else:
            cct._write_incremental_logs('id', str(tmpdir))

        if get_logs_exc is None:
            self._check_logfiles(log_entries, str(tmpdir))

    @pytest.mark.parametrize('get_logs_exc', [None, Exception('error')])
    @pytest.mark.parametrize('build_not_finished', [False, True])
    def test_write_logs_source(self, tmpdir, get_logs_exc, build_not_finished):
        cct = builder_containerbuild.BuildSourceContainerTask(id=1,
                                                              method='buildSourceContainer',
                                                              params='params',
                                                              session='session',
                                                              options='options',
                                                              workdir='workdir')
        log_entries = ['line 1',
                       'line 2']

        (flexmock(osbs.api.OSBS)
            .should_receive('build_not_finished').and_return(build_not_finished))
        if build_not_finished:
            (flexmock(osbs.api.OSBS)
                .should_receive('get_build_reason').and_return('Failed'))

        if get_logs_exc:
            (flexmock(osbs.api.OSBS)
                .should_receive('get_build_logs')
                .and_raise(get_logs_exc))
        else:
            (flexmock(osbs.api.OSBS)
                .should_receive('get_build_logs')
                .and_return(log_entries))

        if get_logs_exc:
            exc_type = builder_containerbuild.ContainerError
            exc_msg = 'Exception while waiting for {what}: {why}'.format(
                what='build logs',
                why=get_logs_exc
            )
        elif build_not_finished:
            exc_type = builder_containerbuild.ContainerError
            exc_msg = 'Build log finished but build still has not finished: Failed.'
        else:
            exc_type = exc_msg = None

        if exc_type is not None:
            with pytest.raises(exc_type) as exc_info:
                cct._write_incremental_logs('id', str(tmpdir))
            assert str(exc_info.value) == exc_msg
        else:
            cct._write_incremental_logs('id', str(tmpdir))

        if get_logs_exc is None:
            self._check_logfiles(log_entries, str(tmpdir))

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

    def _mock_osbs(self, koji_build_id, src, koji_task_id, source=False,
                   build_not_started=False, create_build_args=None,
                   with_osbsvalidationexception=False):
        create_build_args = create_build_args or {}
        create_build_args.setdefault('user', 'owner-name')
        create_build_args.setdefault('target', 'target-name')
        create_build_args.setdefault('scratch', False)

        if not source:
            create_build_args.setdefault('component', 'fedora-docker')
            create_build_args.setdefault('git_uri', src['git_uri'])
            create_build_args.setdefault('git_ref', src['git_ref'])
            create_build_args.setdefault('dependency_replacements', None)
            create_build_args.setdefault('yum_repourls', [])
            create_build_args.setdefault('platforms', ['x86_64'])
            create_build_args.pop('arch_override', None)
        else:
            create_build_args.setdefault('component', 'source_package-source')
            if create_build_args.get('koji_build_id'):
                create_build_args['sources_for_koji_build_id'] = create_build_args['koji_build_id']
                create_build_args.pop('koji_build_id')
            else:
                create_build_args['sources_for_koji_build_id'] = 12345

            if create_build_args.get('koji_build_nvr'):
                create_build_args['sources_for_koji_build_nvr'] =\
                    create_build_args['koji_build_nvr']
                create_build_args.pop('koji_build_nvr')
            else:
                create_build_args['sources_for_koji_build_nvr'] = 'build_nvr'

        if not create_build_args.get('isolated'):
            create_build_args.pop('isolated', None)

        if not create_build_args.get('compose_ids'):
            create_build_args.pop('compose_ids', None)

        if not create_build_args.get('signing_intent'):
            create_build_args.pop('signing_intent', None)

        if not create_build_args.get('userdata'):
            create_build_args.pop('userdata', None)

        if source:
            (flexmock(osbs.api.OSBS)
                .should_receive('create_source_container_build')
                .with_args(koji_task_id=koji_task_id, **create_build_args)
                .times(0 if build_not_started else 1)
                .and_return({}))
        else:
            if with_osbsvalidationexception:
                (flexmock(osbs.api.OSBS)
                    .should_receive('create_binary_container_build')
                    .with_args(koji_task_id=koji_task_id, **create_build_args)
                    .times(1)
                    .and_raise(OsbsValidationException))
            else:
                (flexmock(osbs.api.OSBS)
                    .should_receive('create_binary_container_build')
                    .with_args(koji_task_id=koji_task_id, **create_build_args)
                    .times(0 if build_not_started else 1)
                    .and_return({}))

        (flexmock(osbs.api.OSBS)
            .should_receive('get_build_logs')
            .with_args('os-build-id', follow=True, wait=True)
            .and_return(logs))
        (flexmock(osbs.api.OSBS).should_receive('get_build_name').and_return('os-build-id'))
        (flexmock(osbs.api.OSBS).should_receive('get_build').and_return({}))
        (flexmock(osbs.api.OSBS).should_receive('get_build_reason').and_return('Succeeded'))
        (flexmock(osbs.api.OSBS).should_receive('build_has_succeeded').and_return(True))
        (flexmock(osbs.api.OSBS).should_receive('build_was_cancelled').and_return(False))
        repos_str = '{"unique": ["unique-repo"], "primary": ["primary-repo"]}'
        (flexmock(osbs.api.OSBS)
            .should_receive('get_build_annotations')
            .and_return({'repositories': repos_str}))
        (flexmock(osbs.api.OSBS)
            .should_receive('get_build_labels')
            .and_return({'koji-build-id': koji_build_id}))
        (flexmock(osbs.api.OSBS).should_receive('build_not_finished').and_return(False))
        (flexmock(osbs.api.OSBS).should_receive('cancel_build').never())
        (flexmock(osbs.api.OSBS)
            .should_receive('get_build_error_message')
            .and_return("build error"))

    def _mock_folders(self, tmpdir, dockerfile_content=None, additional_tags_content=None):
        if dockerfile_content is None:
            dockerfile_content = dedent("""\
                FROM fedora

                LABEL name=fedora
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

    @pytest.mark.parametrize(('df_content', 'missing_labels', 'return_val'), (
        ("""\
            FROM fedora
         """,
         ['com.redhat.component (or BZComponent)',
          'version (or Version)',
          'name (or Name)'], None),

        ("""\
            FROM fedora
            LABEL name=fedora
            LABEL com.redhat.component=fedora-docker
         """,
         ['version (or Version)'], None),

        ("""\
            FROM fedora
            LABEL name=fedora
            LABEL version=25
         """,
         ['com.redhat.component (or BZComponent)'], None),

        ("""\
            FROM fedora
            LABEL com.redhat.component=fedora-docker
            LABEL version=25
         """,
         ['name (or Name)'], None),

        ("""\
            FROM fedora
            LABEL name="$NAME_ENV"
            LABEL com.redhat.component=fedora-docker
            LABEL version=25
         """,
         ['name (or Name)'], None),

        ("""\
            FROM fedora
            LABEL name=fedora
            LABEL com.redhat.component="$COMPONENT_ENV"
            LABEL version=25
         """,
         ['com.redhat.component (or BZComponent)'], None),

        ("""\
            FROM fedora
            LABEL name=fedora
            LABEL com.redhat.component=fedora-docker
            LABEL version="$VERSION_ENV"
         """,
         None, ('fedora-docker', None)),

        ("""\
            FROM fedora
            LABEL name=fedora
            LABEL com.redhat.component=fedora-docker
            LABEL version=25
         """,
         None, ('fedora-docker', None)),

        ("""\
            FROM fedora
            LABEL name=fedora
            LABEL com.redhat.component=fedora-docker
            LABEL version=25
            LABEL release="$RELEASE_ENV"
         """,
         None, ('fedora-docker', None)),

        ("""\
            FROM fedora
            LABEL name=fedora
            LABEL com.redhat.component=fedora-docker
            LABEL version=25
            LABEL release=3
         """,
         None, ('fedora-docker', 'fedora-docker-25-3')),
    ))
    def test_checkLabels(self, tmpdir, df_content, missing_labels, return_val):
        cct = builder_containerbuild.BuildContainerTask(id=1,
                                                        method='buildContainer',
                                                        params='params',
                                                        session='session',
                                                        options='options',
                                                        workdir='workdir')
        folder_info = self._mock_folders(str(tmpdir),
                                         dockerfile_content=df_content)
        (flexmock(cct)
            .should_receive('fetchDockerfile')
            .and_return(folder_info['dockerfile_path']))

        if not missing_labels:
            check_return = cct.checkLabels('src', 'build-tag')
            assert check_return == return_val
            return

        with pytest.raises(koji.BuildError) as exc_info:
            cct.checkLabels('src', 'build-tag')

        err_msg = str(exc_info.value)
        assert all(label in err_msg for label in missing_labels)

    @pytest.mark.parametrize(('pkg_info', 'failure'), (
        (None, 'not in list for tag'),
        ({'blocked': True}, 'is blocked for'),
        ({'blocked': False}, None),
    ))
    def test_osbs_build(self, tmpdir, pkg_info, failure):
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
                                                         workdir='workdir')

        (flexmock(task)
            .should_receive('fetchDockerfile')
            .with_args(src['src'], 'build-tag')
            .and_return(folders_info['dockerfile_path']))
        (flexmock(task)
            .should_receive('_write_incremental_logs'))
        (flexmock(task)
            .should_receive('_write_logs'))

        build_args = {'git_branch': 'working'}
        self._mock_osbs(koji_build_id=koji_build_id,
                        src=src,
                        koji_task_id=koji_task_id,
                        build_not_started=bool(failure),
                        create_build_args=deepcopy(build_args))

        if failure:
            with pytest.raises(koji.BuildError) as exc:
                task.handler(src['src'], 'target', opts=build_args)

            assert failure in str(exc.value)

        else:
            task_response = task.handler(src['src'], 'target', opts=build_args)

            assert task_response == {
                'repositories': ['unique-repo', 'primary-repo'],
                'koji_builds': [koji_build_id]
            }

    @pytest.mark.parametrize(('pkg_info', 'failure'), (
        (None, 'not in list for tag'),
        ({'blocked': True}, 'is blocked for'),
        ({'blocked': False}, None),
    ))
    def test_osbs_build_source(self, pkg_info, failure):
        koji_task_id = 123
        last_event_id = 456
        koji_build_id = 999
        create_args = {'koji_build_id': 12345}

        session = self._mock_session(last_event_id, koji_task_id, pkg_info)
        build_json = {'build_id': 12345, 'nvr': 'build_nvr', 'name': 'source_package',
                      'extra': {'image': {}, 'operator-manifests': {}}}
        (session
            .should_receive('getBuild')
            .and_return(build_json))
        (session
            .should_receive('getPackageConfig')
            .with_args('dest-tag', 'source_package-source')
            .and_return(pkg_info))
        options = flexmock(allowed_scms='pkgs.example.com:/*:no')

        task = builder_containerbuild.BuildSourceContainerTask(id=koji_task_id,
                                                               method='buildSourceContainer',
                                                               params='params',
                                                               session=session,
                                                               options=options,
                                                               workdir='workdir')

        (flexmock(task)
            .should_receive('_write_incremental_logs'))
        (flexmock(task)
            .should_receive('_write_logs'))

        self._mock_osbs(koji_build_id=koji_build_id,
                        src=None,
                        koji_task_id=koji_task_id,
                        source=True,
                        create_build_args=create_args.copy(),
                        build_not_started=bool(failure))

        if failure:
            with pytest.raises(koji.BuildError) as exc:
                task.handler('target', opts=create_args)

            assert failure in str(exc.value)

        else:
            task_response = task.handler('target', opts=create_args)

            assert task_response == {
                'repositories': ['unique-repo', 'primary-repo'],
                'koji_builds': [koji_build_id]
            }

    @pytest.mark.parametrize('reason, expected_exc_type', [
        ('signal_cancelled', builder_containerbuild.ContainerCancelled),
        ('build_cancelled', builder_containerbuild.ContainerCancelled),
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

        self._mock_osbs(koji_build_id=koji_build_id,
                        src=src,
                        koji_task_id=koji_task_id,
                        create_build_args={'git_branch': 'working'})

        (flexmock(osbs.api.OSBS).should_receive('build_has_succeeded').and_return(False))
        (flexmock(osbs.api.OSBS)
            .should_receive('build_was_cancelled')
            .and_return(reason == 'build_cancelled' or reason == 'signal_cancelled'))

        if reason == 'signal_cancelled':
            task._incremental_upload_logs = \
                lambda pid: os.kill(os.getpid(), signal.SIGINT)
            (flexmock(osbs.api.OSBS).should_receive('cancel_build').once())

        if reason == 'signal_cancelled' or reason == 'build_cancelled':
            (session.should_receive('cancelTask').once())

        with pytest.raises(expected_exc_type):
            task.handler(src['src'], 'target', {'git_branch': 'working'})

    @pytest.mark.parametrize('reason, expected_exc_type', [
        ('signal_cancelled', builder_containerbuild.ContainerCancelled),
        ('build_cancelled', builder_containerbuild.ContainerCancelled),
        ('failed', builder_containerbuild.ContainerError),
    ])
    def test_createSourceContainer_failure_source(self, reason, expected_exc_type):
        koji_task_id = 123
        last_event_id = 456
        koji_build_id = 999
        create_args = {'koji_build_id': 12345}

        session = self._mock_session(last_event_id, koji_task_id)
        build_json = {'build_id': 12345, 'nvr': 'build_nvr', 'name': 'source_package',
                      'extra': {'image': {}, 'operator-manifests': {}}}
        (session.should_receive('getBuild').and_return(build_json))
        (session
            .should_receive('getPackageConfig')
            .with_args('dest-tag', 'source_package-source')
            .and_return({'blocked': False}))
        options = flexmock(allowed_scms='pkgs.example.com:/*:no')

        task = builder_containerbuild.BuildSourceContainerTask(id=koji_task_id,
                                                               method='buildSourceContainer',
                                                               params='params',
                                                               session=session,
                                                               options=options,
                                                               workdir='workdir')

        self._mock_osbs(koji_build_id=koji_build_id,
                        src=None,
                        koji_task_id=koji_task_id,
                        source=True,
                        create_build_args=create_args.copy())

        (flexmock(osbs.api.OSBS).should_receive('build_has_succeeded').and_return(False))
        (flexmock(osbs.api.OSBS)
            .should_receive('build_was_cancelled')
            .and_return(reason == 'build_cancelled' or reason == 'signal_cancelled'))

        if reason == 'signal_cancelled':
            task._incremental_upload_logs = \
                lambda pid: os.kill(os.getpid(), signal.SIGINT)
            (flexmock(osbs.api.OSBS).should_receive('cancel_build').once())

        if reason == 'signal_cancelled' or reason == 'build_cancelled':
            (session.should_receive('cancelTask').once())

        with pytest.raises(expected_exc_type):
            task.handler('target', create_args)

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
                                                         workdir=str(tmpdir))
        with pytest.raises(koji.BuildError) as exc:
            task.handler(src['src'], 'target', opts={'git_branch': 'working'})
        assert "Target `target` not found" in str(exc.value)

    def test_missing_git_branch(self, tmpdir):
        koji_task_id = 123
        last_event_id = 456

        session = self._mock_session(last_event_id, koji_task_id)
        src = self._mock_git_source()
        task = builder_containerbuild.BuildContainerTask(id=koji_task_id,
                                                         method='buildContainer',
                                                         params='params',
                                                         session=session,
                                                         options={},
                                                         workdir=str(tmpdir))
        with pytest.raises(koji.BuildError) as exc:
            task.handler(src['src'], 'target', opts={})
        assert "Git branch must be specified" in str(exc.value)

    def test_scratch_and_isolated_conflict(self, tmpdir):
        koji_task_id = 123
        last_event_id = 456

        session = self._mock_session(last_event_id, koji_task_id)
        src = self._mock_git_source()
        task = builder_containerbuild.BuildContainerTask(id=koji_task_id,
                                                         method='buildContainer',
                                                         params='params',
                                                         session=session,
                                                         options={},
                                                         workdir=str(tmpdir))
        with pytest.raises(koji.BuildError) as exc:
            task.handler(src['src'], 'target', opts={'scratch': True, 'isolated': True,
                                                     'git_branch': 'working'})
        assert "Build cannot be both isolated and scratch" in str(exc.value)

    def test_get_build_target_failed_source(self, tmpdir):
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
        task = builder_containerbuild.BuildSourceContainerTask(id=koji_task_id,
                                                               method='buildSourceContainer',
                                                               params='params',
                                                               session=session,
                                                               options={},
                                                               workdir=str(tmpdir))
        with pytest.raises(koji.BuildError) as exc:
            task.handler('target', opts={'koji_build_id': 12345})
        assert "Target `target` not found" in str(exc.value)

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
                                                         workdir=str(tmpdir))
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

    @pytest.mark.parametrize('log_upload_raises', (True, False))
    @pytest.mark.parametrize('additional_args', (
        {'koji_parent_build': 'fedora-26-99'},
        {'scratch': True},
        {'scratch': False},
        {'isolated': True},
        {'isolated': False},
        {'release': '13'},
        {'userdata': {'custom': 'userdata'}},
        {'userdata': {}},
    ))
    def test_additional_args(self, tmpdir, log_upload_raises, additional_args):
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
        (flexmock(task)
            .should_receive('_write_incremental_logs'))
        (flexmock(task)
            .should_receive('_write_logs'))

        if log_upload_raises:
            (flexmock(task)
                .should_receive('_incremental_upload_logs')
                .and_raise(koji.ActionNotAllowed))
        else:
            (flexmock(task)
                .should_call('_incremental_upload_logs'))

        build_args = deepcopy(additional_args)
        build_args['git_branch'] = 'working'
        self._mock_osbs(koji_build_id=koji_build_id,
                        src=src,
                        koji_task_id=koji_task_id,
                        create_build_args=deepcopy(build_args))

        task_response = task.handler(src['src'], 'target', opts=build_args)

        assert task_response == {
            'repositories': ['unique-repo', 'primary-repo'],
            'koji_builds': [koji_build_id]
        }

    @pytest.mark.parametrize('log_upload_raises', (True, False))
    @pytest.mark.parametrize('additional_args', (
        {'scratch': True, 'koji_build_id': 12345},
        {'scratch': False, 'koji_build_id': 12345},
        {'scratch': True, 'koji_build_nvr': 'build_nvr'},
        {'scratch': False, 'koji_build_nvr': 'build_nvr'},
        {'signing_intent': 'some intent', 'koji_build_id': 12345},
        {'signing_intent': 'some intent', 'koji_build_nvr': 'build_nvr'},
        {'signing_intent': 'some intent', 'koji_build_nvr': 'build_nvr', 'koji_build_id': 12345},
        {'koji_build_nvr': 'build_nvr', 'koji_build_id': 12345},
        {'userdata': {'custom': 'userdata'}, 'koji_build_id': 12345},
        {'userdata': {}, 'koji_build_id': 12345},
    ))
    def test_additional_args_source(self, log_upload_raises, additional_args):
        koji_task_id = 123
        last_event_id = 456
        koji_build_id = 999

        session = self._mock_session(last_event_id, koji_task_id)

        (session
            .should_receive('getPackageConfig')
            .with_args('dest-tag', 'source_package-source')
            .and_return({'blocked': False}))

        options = flexmock(allowed_scms='pkgs.example.com:/*:no')

        task = builder_containerbuild.BuildSourceContainerTask(id=koji_task_id,
                                                               method='buildSourceContainer',
                                                               params='params',
                                                               session=session,
                                                               options=options,
                                                               workdir='workdir')

        build_json = {'build_id': 12345, 'nvr': 'build_nvr', 'name': 'source_package',
                      'extra': {'image': {}, 'operator-manifests': {}}}
        (session
            .should_receive('getBuild')
            .and_return(build_json))
        (flexmock(task)
            .should_receive('_write_incremental_logs'))
        (flexmock(task)
            .should_receive('_write_logs'))

        if log_upload_raises:
            (flexmock(task)
                .should_receive('_incremental_upload_logs')
                .and_raise(koji.ActionNotAllowed))
        else:
            (flexmock(task)
                .should_call('_incremental_upload_logs'))

        self._mock_osbs(koji_build_id=koji_build_id,
                        src=None,
                        koji_task_id=koji_task_id,
                        source=True,
                        create_build_args=additional_args.copy())

        task_response = task.handler('target', opts=additional_args)

        assert task_response == {
            'repositories': ['unique-repo', 'primary-repo'],
            'koji_builds': [koji_build_id]
        }

    @pytest.mark.parametrize(('isolated', 'release', 'koji_parent_build'), (
        (None, None, None),
        (False, None, None),
        (False, 'test-release', None),
        (True, 'test-relese', 'parent-build'),
    ))
    def test_flatpak_build(self, tmpdir, isolated, release, koji_parent_build):
        task_id = 123
        last_event_id = 456
        koji_build_id = 999

        session = self._mock_session(last_event_id, task_id, {'blocked': False})
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
            'git_branch': 'working',
        }
        if isolated is not None:
            additional_args['isolated'] = isolated
        if release is not None:
            additional_args['release'] = release
        if koji_parent_build is not None:
            additional_args['koji_parent_build'] = koji_parent_build

        self._mock_osbs(koji_build_id=koji_build_id,
                        src=src,
                        koji_task_id=task_id,
                        create_build_args=deepcopy(additional_args),
                        build_not_started=False)

        task_response = task.handler(src['src'], 'target', opts=additional_args)
        assert task_response == {
            'repositories': ['unique-repo', 'primary-repo'],
            'koji_builds': [koji_build_id]
        }

    @pytest.mark.parametrize(('tag', 'release', 'is_oversized'), (
        ('t', None, False),
        ('t'*128, None, False),
        ('t'*129, None, True),
        (None, '1', False),
        (None, '1'*125, False),  # Assumes '25-' prefix for {version}-{release} tag
        (None, '1'*126, True),  # Assumes '25-' prefix for {version}-{release} tag
    ))
    def test_oversized_tags(self, tmpdir, tag, release, is_oversized):
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
                                                         workdir='workdir')

        (flexmock(task)
            .should_receive('fetchDockerfile')
            .with_args(src['src'], 'build-tag')
            .and_return(folders_info['dockerfile_path']))
        (flexmock(task)
            .should_receive('_write_incremental_logs'))
        (flexmock(task)
            .should_receive('_write_logs'))

        additional_args = {'git_branch': 'working'}
        if release:
            additional_args['release'] = release
        self._mock_osbs(koji_build_id=koji_build_id,
                        src=src,
                        koji_task_id=koji_task_id,
                        create_build_args=additional_args.copy(),
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

    @pytest.mark.parametrize(('build_state', 'build_fails'), (
        ('COMPLETE', True),
        ('FAILED', False),
        ('CANCELED', False),
    ))
    @pytest.mark.parametrize(('df_release', 'param_release', 'expected'), (
        ('10', '11', '11'),
        (None, '11', '11'),
        ('10', None, '10'),
    ))
    def test_build_nvr_exists(self, tmpdir, build_state, build_fails, df_release,
                              param_release, expected):
        koji_task_id = 123
        last_event_id = 456
        koji_build_id = 999

        session = self._mock_session(last_event_id, koji_task_id)

        (session
            .should_receive('getBuild')
            .with_args('fedora-docker-25-%s' % expected)
            .and_return({'id': last_event_id, 'state': koji.BUILD_STATES[build_state]}))

        dockerfile_content = dedent("""\
            FROM fedora

            LABEL name=fedora
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
                                                         workdir='workdir')

        (flexmock(task)
            .should_receive('fetchDockerfile')
            .with_args(src['src'], 'build-tag')
            .and_return(folders_info['dockerfile_path']))
        (flexmock(task)
            .should_receive('_write_incremental_logs'))
        (flexmock(task)
            .should_receive('_write_logs'))

        additional_args = {'git_branch': 'working'}
        if param_release:
            additional_args['release'] = param_release

        self._mock_osbs(koji_build_id=koji_build_id,
                        src=src,
                        koji_task_id=koji_task_id,
                        create_build_args=additional_args.copy(),
                        build_not_started=build_fails)

        if build_fails:
            with pytest.raises(koji.BuildError) as exc_info:
                task.handler(src['src'], 'target', opts=additional_args)
            assert 'already exists' in str(exc_info.value)
        else:
            task.handler(src['src'], 'target', opts=additional_args)

    @pytest.mark.parametrize(('create_args', 'build_types', 'cause'), (
        ({'koji_build_id': 12345},
         ['image', 'operator-manifests'], 'doesnt exist'),
        ({'koji_build_nvr': 'image_build-1-1'},
         ['image', 'operator-manifests'], 'doesnt exist'),
        ({'koji_build_nvr': 'image_build-1-1', 'koji_build_id': 12345},
         ['image', 'operator-manifests'], 'doesnt exist'),
        ({'koji_build_nvr': 'image_build-1-1', 'koji_build_id': 12345},
         ['image', 'operator-manifests'], 'mismatch'),
        ({'koji_build_nvr': 'rpm_build-1-1', 'koji_build_id': 12345},
         ['rpm', 'operator-manifests'], 'wrong type'),
        ({'koji_build_nvr': 'image_build-source-1-1', 'koji_build_id': 12345},
         ['image', 'operator-manifests'], 'source build'),
    ))
    def test_source_build_info(self, create_args, build_types, cause):
        koji_task_id = 123
        last_event_id = 456
        koji_build_id = 999
        different_build_id = 54321

        provided_nvr = create_args.get('koji_build_nvr')
        provided_id = create_args.get('koji_build_id')
        provided_name = None
        if provided_nvr:
            provided_name = provided_nvr.rsplit('-', 2)[0]

        session = self._mock_session(last_event_id, koji_task_id)

        typeinfo_dict = {b_type: {} for b_type in build_types}
        build_json = {'build_id': provided_id, 'nvr': provided_nvr, 'name': provided_name,
                      'extra': typeinfo_dict}
        if cause == 'source build':
            build_json['extra']['image']['sources_for_nvr'] = 'some source'

        (session
            .should_receive('getBuild')
            .and_return(build_json))

        if cause == 'doesnt exist':
            (session
                .should_receive('getBuild')
                .and_return({}))

            build_id = provided_nvr or provided_id
            log_message = "specified source build '{}' doesn't exist".format(build_id)

        elif cause == 'mismatch':
            (session
                .should_receive('getBuild')
                .and_return({'build_id': different_build_id, 'nvr': provided_nvr,
                             'name': provided_name}))

            log_message = (
                'koji_build_id {} does not match koji_build_nvr {} with id {}. '
                'When specifying both an id and an nvr, they should point to the same image build'
                .format(provided_id, provided_nvr, different_build_id))

        elif cause == 'wrong type':
            log_message = ('koji build {} is not image build which source container requires'
                           .format(provided_nvr))

        elif cause == 'source build':
            log_message = ('koji build {} is source container build, source container can not '
                           'use source container build image'.format(provided_nvr))

        options = flexmock(allowed_scms='pkgs.example.com:/*:no')

        task = builder_containerbuild.BuildSourceContainerTask(id=koji_task_id,
                                                               method='buildSourceContainer',
                                                               params='params',
                                                               session=session,
                                                               options=options,
                                                               workdir='workdir')
        (flexmock(task)
            .should_receive('_write_incremental_logs'))
        (flexmock(task)
            .should_receive('_write_logs'))

        self._mock_osbs(koji_build_id=koji_build_id,
                        src=None,
                        koji_task_id=koji_task_id,
                        source=True,
                        create_build_args=create_args.copy(),
                        build_not_started=True)

        with pytest.raises(koji.BuildError) as exc_info:
            task.handler('target', opts=create_args)
        assert log_message in str(exc_info.value)

    @pytest.mark.parametrize(('additional_args', 'raises'), (
        ({}, False),
        ({'compose_ids': [1, 2]}, False),
        ({'signing_intent': 'intent1'}, False),
        ({'compose_ids': [1, 2], 'signing_intent': 'intent1'}, True),
        ({'compose_ids': [1, 2], 'yum_repourls': ['www.repo.com']}, False)
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
                                                         workdir='workdir')

        (flexmock(task)
            .should_receive('fetchDockerfile')
            .with_args(src['src'], 'build-tag')
            .and_return(folders_info['dockerfile_path']))
        (flexmock(task)
            .should_receive('_write_incremental_logs'))
        (flexmock(task)
            .should_receive('_write_logs'))

        build_args = deepcopy(additional_args)
        build_args['git_branch'] = 'working'
        self._mock_osbs(koji_build_id=koji_build_id,
                        src=src,
                        koji_task_id=koji_task_id,
                        build_not_started=raises,
                        create_build_args=deepcopy(build_args))

        if raises:
            with pytest.raises(koji.BuildError):
                task.handler(src['src'], 'target', opts=build_args)
        else:
            task_response = task.handler(src['src'], 'target', opts=build_args)

            assert task_response == {
                'repositories': ['unique-repo', 'primary-repo'],
                'koji_builds': [koji_build_id]
            }

    @pytest.mark.parametrize(('additional_args', 'raises'), (
        ({'scratch': True, 'arch_override': 'x86_64'}, False),
        ({'scratch': True, 'arch_override': ''}, False),
        ({'scratch': False, 'arch_override': 'x86_64'}, True),
        ({'scratch': False, 'arch_override': ''}, False),
        ({'isolated': True, 'arch_override': 'x86_64'}, False),
        ({'isolated': True, 'arch_override': ''}, False),
        ({'isolated': False, 'arch_override': 'x86_64'}, True),
        ({'isolated': False, 'arch_override': ''}, False),
        ({'scratch': True, 'isolated': True, 'arch_override': 'x86_64'}, True),
        ({'scratch': True, 'isolated': True, 'arch_override': ''}, True),
        ({'scratch': False, 'isolated': True, 'arch_override': 'x86_64'}, False),
        ({'scratch': False, 'isolated': True, 'arch_override': ''}, False),
        ({'scratch': True, 'isolated': False, 'arch_override': 'x86_64'}, False),
        ({'scratch': True, 'isolated': False, 'arch_override': ''}, False),
        ({'scratch': False, 'isolated': False, 'arch_override': 'x86_64'}, True),
        ({'scratch': False, 'isolated': False, 'arch_override': ''}, False),
    ))
    def test_arch_override(self, tmpdir, additional_args, raises):
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
        (flexmock(task)
            .should_receive('_write_incremental_logs'))
        (flexmock(task)
            .should_receive('_write_logs'))

        build_args = deepcopy(additional_args)
        build_args['git_branch'] = 'working'
        self._mock_osbs(koji_build_id=koji_build_id,
                        src=src,
                        koji_task_id=koji_task_id,
                        build_not_started=raises,
                        create_build_args=deepcopy(build_args))

        if raises:
            with pytest.raises(koji.BuildError):
                task.handler(src['src'], 'target', opts=build_args)
        else:
            task_response = task.handler(src['src'], 'target', opts=build_args)

            assert task_response == {
                'repositories': ['unique-repo', 'primary-repo'],
                'koji_builds': [koji_build_id]
            }

    @pytest.mark.parametrize('arg_name, arg_value, expected_types', [
        ('src', None, ['string']),
        ('src', 123, ['string']),

        ('target', None, ['string']),
        ('target', 123, ['string']),

        ('opts', '{"a": "b"}', ['object']),
        ('opts', 123, ['object']),
        ('opts', [], ['object']),
        ('opts', None, ['object']),
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

    @pytest.mark.parametrize('arg_name, arg_value, expected_types', [
        ('target', None, ['string']),
        ('target', 123, ['string']),

        ('opts', '{"a": "b"}', ['object']),
        ('opts', 123, ['object']),
        ('opts', [], ['object']),
        ('opts', None, ['object']),
    ])
    def test_schema_validation_invalid_arg_types_source(self, arg_name, arg_value, expected_types):
        task = builder_containerbuild.BuildSourceContainerTask(id=1,
                                                               method='buildSourceContainer',
                                                               params={},
                                                               session=None,
                                                               options=None,
                                                               workdir='not used')
        task_args = {
            'target': arg_value if arg_name == 'target' else 'xyz',
            'opts': arg_value if arg_name == 'opts' else None,
        }

        with pytest.raises(jsonschema.ValidationError) as exc_info:
            task.handler(**task_args)

        expected_types_str = ', '.join('{!r}'.format(t) for t in expected_types)
        err_msg = 'is not of type {}'.format(expected_types_str)
        assert err_msg in str(exc_info.value)

    @pytest.mark.parametrize(('property_name', 'property_value', 'expected_types'), [
        ('scratch', 'true', ['boolean']),
        ('scratch', 1, ['boolean']),
        ('scratch', None, ['boolean']),

        ('isolated', 'true', ['boolean']),
        ('isolated', 1, ['boolean']),
        ('isolated', None, ['boolean']),

        ('dependency_replacements', 'gomod:foo.bar/project:1', ['array', 'null']),
        ('dependency_replacements', ['gomod:foo.bar/project:1', 1], ['string']),

        ('yum_repourls', 'just.one.url', ['array', 'null']),
        ('yum_repourls', ['some.url', 1], ['string']),

        ('git_branch', 123, ['string', 'null']),
        ('koji_parent_build', 123, ['string', 'null']),
        ('release', 123, ['string', 'null']),

        ('flatpak', 'true', ['boolean']),
        ('flatpak', 1, ['boolean']),
        ('flatpak', None, ['boolean']),

        ('compose_ids', 1, ['array', 'null']),
        ('compose_ids', [1, '2'], ['integer']),
        ('compose_ids', [1.5], ['integer']),

        ('signing_intent', 123, ['string', 'null']),
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

    @pytest.mark.parametrize(('property_name', 'property_value', 'expected_types', 'build_opts'), [
        ('scratch', 'true', ['boolean'],
         {'koji_build_id': 12345}),
        ('scratch', 1, ['boolean'],
         {'koji_build_id': 12345}),
        ('scratch', None, ['boolean'],
         {'koji_build_id': 12345}),
        ('signing_intent', 123, ['string'],
         {'koji_build_id': 12345}),
        ('signing_intent', None, ['string'],
         {'koji_build_id': 12345}),
        ('koji_build_id', None, ['integer'],
         {}),
        ('koji_build_id', 'string', ['integer'],
         {}),
        ('koji_build_nvr', 123, ['string'],
         {}),
        ('koji_build_nvr', None, ['string'],
         {}),
    ])
    def test_schema_validation_invalid_type_for_opts_property_source(self,
                                                                     property_name,
                                                                     property_value,
                                                                     expected_types,
                                                                     build_opts):
        task = builder_containerbuild.BuildSourceContainerTask(id=1,
                                                               method='buildSourceContainer',
                                                               params={},
                                                               session=None,
                                                               options=None,
                                                               workdir='not used')
        build_opts[property_name] = property_value

        with pytest.raises(jsonschema.ValidationError) as exc_info:
            task.handler('target', build_opts)

        expected_types_str = ', '.join('{!r}'.format(t) for t in expected_types)
        err_msg = 'is not of type {}'.format(expected_types_str)
        assert err_msg in str(exc_info.value)

    @pytest.mark.parametrize(('build_opts', 'valid'), [
        ({'scratch': False,
          'isolated': False,
          'dependency_replacements': None,
          'yum_repourls': None,
          'git_branch': 'working',
          'koji_parent_build': None,
          'release': None,
          'flatpak': False,
          'compose_ids': None,
          'signing_intent': None},
         True),

        ({'scratch': False,
          'isolated': False,
          'dependency_replacements': ['gomod:foo/bar:1', 'gomod:foo/baz:2'],
          'yum_repourls': ['url.1', 'url.2'],
          'git_branch': 'working',
          'koji_parent_build': 'some-or-other',
          'release': 'v8',
          'flatpak': False,
          'compose_ids': [1, 2, 3],
          'signing_intent': 'No, I do not intend to sign anything.'},
         True),

        ({'version': '1.2',  # invalid
          'name': 'foo',     # invalid
          'git_branch': 'working'},
         False),
    ])
    def test_schema_validation_valid_options_container(self, build_opts, valid, tmpdir):
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
            .should_receive('createContainer')
            .and_return({'repositories': 'something.somewhere'}))

        if valid:
            task.handler(src['src'], 'target', build_opts)
        else:
            with pytest.raises(jsonschema.exceptions.ValidationError):
                task.handler(src['src'], 'target', build_opts)

    @pytest.mark.parametrize('scratch', (True, False))
    @pytest.mark.parametrize('signing_intent', (None, 'No, I do not intend to sign anything.'))
    @pytest.mark.parametrize('build_opts', [
        {'koji_build_id': 12345},
        {'koji_build_nvr': 'build_nvr'},
        {'koji_build_id': 12345,
         'koji_build_nvr': 'build_nvr'},
    ])
    def test_schema_validation_valid_options_sourcecontainer(self, tmpdir, scratch,
                                                             signing_intent, build_opts):
        koji_task_id = 123
        last_event_id = 456
        source_koji_id = 12345
        source_koji_nvr = 'build_nvr'
        session = self._mock_session(last_event_id, koji_task_id)
        build_json = {'build_id': source_koji_id, 'nvr': source_koji_nvr, 'name': 'package',
                      'extra': {'image': {}, 'operator-manifests': {}}}
        (session
            .should_receive('getBuild')
            .and_return(build_json))

        task = builder_containerbuild.BuildSourceContainerTask(id=koji_task_id,
                                                               method='buildSourceContainer',
                                                               params={},
                                                               session=session,
                                                               options={},
                                                               workdir=str(tmpdir))

        (flexmock(task)
            .should_receive('createSourceContainer')
            .and_return({'repositories': 'something.somewhere'}))

        if scratch:
            build_opts['scratch'] = scratch
        if signing_intent:
            build_opts['signing_intent'] = signing_intent

        task.handler('target', build_opts)

    @pytest.mark.parametrize('annotations', (
        {},
        {'koji_task_annotations_whitelist': '[]'},
        {'koji_task_annotations_whitelist': '[]', 'remote_source_url': 'stub_url'},
        {'koji_task_annotations_whitelist': '["remote_source_url"]',
         'remote_source_url': 'stub_url'},
        {'remote_source_url': 'stub_url'},
        {'a': '1', 'b': '2'},
        {'koji_task_annotations_whitelist': '["a", "b"]', 'a': '1', 'b': '2', 'c': '3'},
        {'koji_task_annotations_whitelist': '["a", "b"]', 'a': '1', 'c': '3'}
        ))
    def test_upload_annotations(self, tmpdir, annotations):
        def mock_incremental_upload(session, fname, fd, uploadpath, logger=None):
            with open(os.path.join(uploadpath, fname), 'w') as f:
                data = fd.read()
                f.write(data)

        builder_containerbuild.incremental_upload = mock_incremental_upload

        annotations_file = tmpdir.join('build_annotations.json').strpath
        cct = builder_containerbuild.BuildContainerTask(id=1,
                                                        method='buildContainer',
                                                        params='params',
                                                        session='session',
                                                        workdir='workdir',
                                                        options='options')
        flexmock(cct).should_receive('getUploadPath').and_return(tmpdir.strpath)

        cct.upload_build_annotations(annotations)
        whitelist = annotations.get('koji_task_annotations_whitelist')
        if whitelist:
            whitelist = json.loads(whitelist)

        if not whitelist or len(annotations) < 2:
            assert not os.path.exists(annotations_file)
        else:
            assert os.path.exists(annotations_file)
            with open(annotations_file) as f:
                build_annotations = json.load(f)
            for key, value in build_annotations.items():
                assert key in whitelist
                assert value == annotations[key]
            for item in whitelist:
                if item in annotations:
                    assert item in build_annotations
                else:
                    assert item not in build_annotations

    def test_raise_OsbsValidationException(self, tmpdir):
        df_content = """\
            FROM fedora
            LABEL com.redhat.component="fedora-docker" \
                  name="osbs-test/reject-hyphen-in-version-label" \
                  version="reject-hyphen.in.version.label"
        """
        folder_info = self._mock_folders(str(tmpdir),
                                         dockerfile_content=df_content)
        koji_task_id = 123
        last_event_id = 456
        koji_build_id = 999

        session = self._mock_session(last_event_id, koji_task_id)
        src = self._mock_git_source()
        options = flexmock(allowed_scms='pkgs.example.com:/*:no')

        builder_containerbuild.incremental_upload = mock_incremental_upload

        task = builder_containerbuild.BuildContainerTask(id=koji_task_id,
                                                         method='buildContainer',
                                                         params='params',
                                                         session=session,
                                                         options=options,
                                                         workdir='workdir')

        (flexmock(task)
            .should_receive('fetchDockerfile')
            .with_args(src['src'], 'build-tag')
            .and_return(folder_info['dockerfile_path']))
        (flexmock(task)
            .should_receive('_write_incremental_logs'))
        (flexmock(task)
            .should_receive('_write_logs'))

        build_args = {'git_branch': 'working'}
        self._mock_osbs(koji_build_id=koji_build_id,
                        src=src,
                        koji_task_id=koji_task_id,
                        create_build_args=deepcopy(build_args),
                        with_osbsvalidationexception=True)
        with pytest.raises(builder_containerbuild.ContainerError):
            task.handler(src['src'], 'target', opts=build_args)

    def test_user_warnings(self, tmpdir):
        log_entries = [
            'normal log',
            'log - USER_WARNING - {"message": "message"}',
            'log - USER_WARNING - {"message": "message"}',
            'log - USER_WARNING - {"message": "another_message"}',
            'another log',
        ]

        koji_task_id = 123
        last_event_id = 456
        koji_build_id = 999

        session = self._mock_session(last_event_id, koji_task_id)
        folders_info = self._mock_folders(str(tmpdir))
        src = self._mock_git_source()
        options = flexmock(allowed_scms='pkgs.example.com:/*:no')

        builder_containerbuild.incremental_upload = mock_incremental_upload

        task = builder_containerbuild.BuildContainerTask(id=koji_task_id,
                                                         method='buildContainer',
                                                         params='params',
                                                         session=session,
                                                         options=options,
                                                         workdir=str(tmpdir))

        (flexmock(task)
            .should_receive('fetchDockerfile')
            .with_args(src['src'], 'build-tag')
            .and_return(folders_info['dockerfile_path']))

        build_args = {'git_branch': 'working'}
        self._mock_osbs(koji_build_id=koji_build_id,
                        src=src,
                        koji_task_id=koji_task_id,
                        create_build_args=deepcopy(build_args))

        (flexmock(osbs.api.OSBS)
            .should_receive('get_build_logs')
            .with_args('os-build-id', follow=True, wait=True)
            .and_return(log_entries))

        task_response = task.handler(src['src'], 'target', opts=build_args)
        expected_user_warnings = ['message', 'another_message']
        found_user_warnings = task_response['user_warnings']

        assert sorted(found_user_warnings) == sorted(expected_user_warnings)

    def test_user_warnings_source(self, tmpdir):
        log_entries = [
            'normal log',
            'log - USER_WARNING - {"message": "message"}',
            'log - USER_WARNING - {"message": "message"}',
            'log - USER_WARNING - {"message": "another_message"}',
            'another log',
        ]

        koji_task_id = 123
        last_event_id = 456
        koji_build_id = 999
        create_args = {'koji_build_id': 12345}

        session = self._mock_session(last_event_id, koji_task_id, {'blocked': False})
        build_json = {'build_id': 12345, 'nvr': 'build_nvr', 'name': 'source_package',
                      'extra': {'image': {}, 'operator-manifests': {}}}
        (session
            .should_receive('getBuild')
            .and_return(build_json))
        (session
            .should_receive('getPackageConfig')
            .with_args('dest-tag', 'source_package-source')
            .and_return({'blocked': False}))
        options = flexmock(allowed_scms='pkgs.example.com:/*:no')

        task = builder_containerbuild.BuildSourceContainerTask(id=koji_task_id,
                                                               method='buildSourceContainer',
                                                               params='params',
                                                               session=session,
                                                               options=options,
                                                               workdir=str(tmpdir))

        self._mock_osbs(koji_build_id=koji_build_id,
                        src=None,
                        koji_task_id=koji_task_id,
                        source=True,
                        create_build_args=create_args.copy(),
                        build_not_started=False)

        (flexmock(osbs.api.OSBS)
            .should_receive('get_build_logs')
            .with_args('os-build-id', follow=True, wait=True)
            .and_return(log_entries))

        task_response = task.handler('target', opts=create_args)
        expected_user_warnings = ['message', 'another_message']
        found_user_warnings = task_response['user_warnings']

        assert sorted(found_user_warnings) == sorted(expected_user_warnings)
