import json
import tempfile
import io
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from api.models import Project
from api.projects import register_project_directory, get_project, register_ingested_project
from api.search_index import stage_project_import, publish_document_import, search_project_index, build_project_index, connect_index
from api.document_versions import list_editions, scope_project


class UploadIntakeTests(TestCase):
    def setUp(self):
        Project.objects.all().delete()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings_override = override_settings(BASE_DIR=self.root)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.user = get_user_model().objects.create_user('upload-admin', password='password', is_staff=True)
        self.client.force_login(self.user)

    def upload(self, paths, contents=None, **target):
        contents = contents or [b'# API\nfield A required'] * len(paths)
        return self.client.post('/api/ingestion/upload/', {
            'paths': json.dumps(paths), 'files': [SimpleUploadedFile(Path(path).name, content) for path, content in zip(paths, contents)],
            **target,
        })

    def test_folder_upload_new_project_append_and_publish_versions(self):
        response = self.upload(['规则/上市规则2025.md', '规则/上市规则2026.md'],
                               ['# 上市规则\n2025旧规则'.encode(), '# 上市规则\n2026新规则'.encode()], projectName='交易规则')
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertFalse(Project.objects.exists())  # Upload alone does not register/query new knowledge.
        self.assertEqual(len(data['files']), 2)
        self.assertEqual(data['files'][0]['displayName'], '规则/上市规则2025.md')
        project = data['target']
        more = self.upload(['其他.txt'], projectName='交易规则', projectDir=project['path'])
        self.assertEqual(more.status_code, 200)
        self.assertEqual(more.json()['target']['path'], project['path'])
        refs = [row['sourceFile'] for row in data['files']]
        stage = self.root / 'stage.db'
        _, baseline = stage_project_import(project, stage, refs)
        assignments = [{'sourceFile': row['sourceFile'], 'documentName': '上市规则', 'versionLabel': str(2025 + index), 'setDefault': index == 1} for index, row in enumerate(data['files'])]
        publish_document_import(project, stage, baseline, {'objects': []}, source_assignments=assignments)
        group, = list_editions(project)['documents']
        self.assertEqual(len(group['versions']), 2)
        self.assertEqual({row['versionLabel'] for row in search_project_index(project, '上市规则')['results']}, {'2026'})
        old = next(v for v in group['versions'] if v['versionLabel'] == '2025')
        self.assertTrue(search_project_index(scope_project(project, {group['id']: old['editionId']}), '旧规则')['results'])
        self.assertTrue(all('上传文件' not in row['headingPath'] for row in search_project_index(project, '上市规则')['results']))

    def test_existing_project_upload_preserves_original_and_repeated_upload_is_idempotent(self):
        raw = self.root / 'project' / 'raw' / 'sources'
        raw.mkdir(parents=True)
        original = raw / '规则.txt'
        original.write_text('原有规则', encoding='utf-8')
        project = register_project_directory(raw.parents[1], '交易规则', raw, build_index=False)
        first = self.upload(['规则.txt'], [b'new rules'], projectId=project['id']).json()
        second = self.upload(['规则.txt'], [b'new rules'], projectId=project['id']).json()
        self.assertEqual(first['files'][0]['sourceFile'], second['files'][0]['sourceFile'])
        self.assertEqual(original.read_text(encoding='utf-8'), '原有规则')
        self.assertEqual(len(list(raw.rglob('*.txt'))), 2)
        scan = self.client.post('/api/ingestion/sources/', json.dumps({'projectId': project['id']}), content_type='application/json')
        self.assertEqual(scan.status_code, 200)
        self.assertEqual(len(scan.json()['files']), 2)

    def test_legacy_registered_project_only_marks_added_files_as_new(self):
        from api.ingestion_files import scan_ingestion_sources
        raw = self.root / 'legacy' / 'raw' / 'sources'
        raw.mkdir(parents=True)
        (raw / '已入库.md').write_text('# 已有规则', encoding='utf-8')
        project = {'path': str(raw.parents[1]), 'rawDir': str(raw)}
        build_project_index(project)
        connection = connect_index(project, create=True)
        try:
            with connection:
                connection.execute('DROP TABLE document_editions')
        finally:
            connection.close()
        (raw.parents[1] / 'document-history' / 'catalog.json').unlink()
        (raw / '新增.md').write_text('# 新规则', encoding='utf-8')
        states = {row['displayName']: row['status'] for row in scan_ingestion_sources(project)['files']}
        self.assertEqual(states, {'已入库.md': 'indexed', '新增.md': 'new'})

    def test_invalid_upload_is_rejected_and_permissions_apply(self):
        for path in ['../outside.txt', '/absolute.txt', 'C:/outside.txt', 'folder/../outside.txt', 'NUL.txt', 'bad.exe']:
            with self.subTest(path=path):
                self.assertEqual(self.upload([path], projectName='测试').status_code, 400)
        self.assertEqual(self.upload(['a.txt', 'A.txt'], projectName='测试').status_code, 400)
        self.assertFalse((self.root / 'data' / 'upload-projects').exists())
        self.user.is_staff = False
        self.user.save()
        self.assertEqual(self.upload(['a.txt'], projectName='测试').status_code, 403)
        self.client.logout()
        self.assertEqual(self.upload(['a.txt'], projectName='测试').status_code, 401)

    def test_registration_only_and_rename_remove_restore_do_not_reingest_or_delete_files(self):
        root = self.root / 'ready-project'
        raw = root / 'resources'
        raw.mkdir(parents=True)
        (raw / '原文.md').write_text('# 规则', encoding='utf-8')
        with patch('api.projects.build_project_index', side_effect=AssertionError('registration must not reingest')):
            response = self.client.post('/api/projects/register/', json.dumps({'projectDir': str(root), 'projectName': '原名'}), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        project = response.json()['registeredProject']
        renamed = self.client.patch('/api/projects/', json.dumps({'id': project['id'], 'name': '新名称'}), content_type='application/json')
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(get_project(project['id'])['name'], '新名称')
        self.assertEqual(get_project(project['id'])['path'], str(root.resolve()))
        removed = self.client.delete('/api/projects/', json.dumps({'id': project['id']}), content_type='application/json')
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.json()['projects'], [])
        self.assertTrue((raw / '原文.md').exists())
        restored = register_project_directory(root, '恢复登记', raw, build_index=False)
        self.assertEqual(restored['id'], project['id'])

    def test_browser_upload_does_not_use_missing_old_server_path_and_keeps_project_id(self):
        old = self.root / 'old-server-unavailable' / '交易规则'
        record = Project.objects.create(name='交易规则', root_path=str(old))
        response = self.upload(['沪市/规则.md'], ['# 规则\n新版内容'.encode()], projectId=str(record.id))
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertFalse(old.exists())
        self.assertEqual(get_project(str(record.id))['path'], str(old))
        target = data['target']
        self.assertTrue(Path(target['path']).is_relative_to(self.root / 'data' / 'upload-projects'))
        self.assertEqual(target['id'], str(record.id))
        refs = [row['sourceFile'] for row in data['files']]
        stage = self.root / 'repair-stage.db'
        _, baseline = stage_project_import(target, stage, refs)
        publish_document_import(target, stage, baseline, {'objects': []})
        job = {'projectDir': target['path'], 'rawDir': target['rawDir'], 'exportDir': target['exportDir'],
               'request': {'targetProjectId': str(record.id), 'originalProjectPath': str(old)}}
        saved = register_ingested_project(job)
        self.assertEqual(saved['id'], str(record.id))
        self.assertEqual(Project.objects.count(), 1)
        self.assertEqual(get_project(str(record.id))['path'], target['path'])

    @override_settings(DATA_UPLOAD_MAX_NUMBER_FILES=100)
    def test_230_nested_files_in_batches_work_with_normal_multipart_limits(self):
        from api.ingestion_files import scan_ingestion_sources
        paths = [f'层级{index // 25}/子目录/规则{index}.md' for index in range(230)]
        target, all_refs = None, []
        for start in range(0, len(paths), 20):
            options = {'projectName': '大目录'}
            if target:
                options['projectDir'] = target['path']
            response = self.upload(paths[start:start + 20], **options)
            self.assertEqual(response.status_code, 200, response.content)
            data = response.json()
            if target:
                self.assertEqual(data['target']['path'], target['path'])
            target = data['target']
            all_refs.extend(row['displayName'] for row in data['files'])
        self.assertEqual(set(all_refs), set(paths))
        scanned = scan_ingestion_sources(target)
        self.assertEqual(len(scanned['files']), 230)
        self.assertEqual({row['displayName'] for row in scanned['files']}, set(paths))

    def test_same_subdirectory_hierarchy_and_duplicate_basenames_as_server_sources(self):
        from api.search_index import get_ingestion_documents
        paths = ['沪市/公共.md', '沪市/上市/公共.md', '深市/公共.md']
        contents = [f'# 共同章节\n规则{index}'.encode() for index in range(3)]
        uploaded = self.upload(paths, contents, projectName='目录层级').json()
        remote = uploaded['target']
        build_project_index(remote)
        raw = self.root / 'direct-source'
        for ref, content in zip(paths, contents):
            destination = raw / ref
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        local = {'path': str(self.root / 'direct-project'), 'rawDir': str(raw)}
        build_project_index(local)
        headings = lambda project: sorted(section['headingPath'] for doc in get_ingestion_documents(project) for section in doc['sections'])
        self.assertEqual(headings(remote), headings(local))
        self.assertEqual(len(get_ingestion_documents(remote)), 3)
        response = self.client.post('/api/ingestion/sources/', json.dumps({
            'mode': 'server', 'sourceDir': str(raw), 'projectName': '本地目录'
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual({row['sourceFile'] for row in response.json()['files']}, set(paths))
        self.assertEqual(response.json()['target']['rawDir'], str(raw))
        self.assertNotEqual(response.json()['target']['path'], str(raw))
        self.assertFalse((raw / '.index').exists())

    def test_upload_request_retry_reuses_session_storage_before_first_response(self):
        options = {'projectName': '重试', 'uploadSessionId': 'b' * 32}
        first = self.upload(['规则.txt'], **options).json()
        repeated = self.upload(['规则.txt'], **options).json()
        self.assertEqual(first['target']['path'], repeated['target']['path'])
        self.assertEqual(first['files'][0]['sourceFile'], repeated['files'][0]['sourceFile'])

    def test_deep_windows_upload_paths_survive_scan_index_and_publish(self):
        from api.file_paths import fs_path
        import shutil
        # The upload prefix makes a valid source tree exceed legacy Windows MAX_PATH.
        ref = '/'.join(['很长的子目录名称' * 3] * 4 + ['同名规则.md'])
        response = self.upload([ref], ['# 长路径规则\n目录内容。'.encode()], projectName='深层目录')
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        project = data['target']
        self.addCleanup(lambda: shutil.rmtree(fs_path(project['path']), ignore_errors=True))
        self.assertEqual(data['files'][0]['displayName'], ref)
        stage = self.root / 'long-path-stage.db'
        _, baseline = stage_project_import(project, stage, [data['files'][0]['sourceFile']])
        self.assertTrue(publish_document_import(project, stage, baseline, {'objects': []})['ok'])
        results = search_project_index(project, '长路径规则')['results']
        self.assertTrue(results)
        self.assertIn('很长的子目录名称', results[0]['headingPath'])

    def test_relocated_job_still_blocks_removing_its_original_registration(self):
        from api import views
        record = Project.objects.create(name='旧项目', root_path=str(self.root / 'unavailable'))
        data = self.upload(['规则.txt'], projectId=str(record.id)).json()
        job = {'id': 'relocated-test', 'status': 'running', 'startedAt': 1,
               'projectDir': data['target']['path'], 'request': {'targetProjectId': str(record.id)}}
        views.save_job(job)
        removed = self.client.delete('/api/projects/', json.dumps({'id': str(record.id)}), content_type='application/json')
        self.assertEqual(removed.status_code, 409)

    def test_worker_publishes_uploaded_files_before_relocating_registration(self):
        from api import views
        from types import SimpleNamespace
        old = self.root / 'old-server-path'
        record = Project.objects.create(name='原项目显示名', root_path=str(old))
        data = self.upload(['子目录/规则.md'], projectId=str(record.id)).json()
        target = data['target']
        payload_path = Path(target['path']) / '.index' / 'worker.json'
        _, baseline = stage_project_import(target, payload_path.with_suffix('.db'), [data['files'][0]['sourceFile']])
        payload_path.write_text(json.dumps({'objects': [], 'documentImport': {'baseline': baseline}}), encoding='utf-8')
        job = {'id': 'relocated-worker', 'status': 'running', 'startedAt': 1, 'logs': [], 'command': ['unused'],
               'timeoutSeconds': 30, 'progress': {'current': 1, 'total': 1}, 'projectName': '原项目显示名',
               'projectDir': target['path'], 'rawDir': target['rawDir'], 'exportDir': target['exportDir'],
               'ingestionOutput': str(payload_path),
               'request': {'targetProjectId': str(record.id), 'originalProjectPath': str(old)}}
        views.save_job(job)
        self.assertEqual(get_project(str(record.id))['path'], str(old))
        process = SimpleNamespace(stdout=io.StringIO(''), poll=lambda: 0, wait=lambda: 0)
        with patch('api.views.subprocess.Popen', return_value=process):
            views.run_ingestion_job(job['id'], self.root / 'unused.py')
        result = views.get_job(job['id'])
        self.assertEqual(result['status'], 'succeeded', result.get('error'))
        self.assertEqual(result['registeredProject']['id'], str(record.id))
        self.assertEqual(get_project(str(record.id))['path'], target['path'])
        self.assertEqual(Project.objects.count(), 1)
