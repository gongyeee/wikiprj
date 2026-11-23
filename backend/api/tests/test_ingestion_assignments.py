import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from api.document_versions import (
    list_editions, scan_ingestion_sources, validate_source_assignments, scope_project,
)
from api.search_index import (
    build_project_index, stage_project_import, publish_document_import, search_project_index,
)


class IngestionAssignmentTests(SimpleTestCase):
    def make_project(self, directory):
        root = Path(directory)
        raw = root / 'raw'
        raw.mkdir()
        (raw / '文档A_1.md').write_text('# 创建订单\n字段A必填。旧版独有。', encoding='utf-8')
        (raw / '文档A_2.md').write_text('# 创建订单\n字段A选填。', encoding='utf-8')
        return {'path': str(root), 'rawDir': str(raw)}

    def assignments(self):
        return [
            {'sourceFile': '文档A_1.md', 'documentName': '文档A', 'versionLabel': '1'},
            {'sourceFile': '文档A_2.md', 'documentName': '文档A', 'versionLabel': '2', 'setDefault': True},
        ]

    def test_scan_is_read_only_and_first_ingestion_publishes_user_selected_grouping(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self.make_project(directory)
            scan = scan_ingestion_sources(project)
            self.assertEqual(len(scan['files']), 2)
            self.assertFalse((Path(directory) / '.index').exists())
            self.assertFalse((Path(directory) / 'document-history').exists())
            assignments = validate_source_assignments(project, self.assignments())
            path = Path(directory) / 'stage.db'
            staged, baseline = stage_project_import(project, path)
            self.assertEqual(list_editions(project)['documents'], [])
            self.assertTrue(publish_document_import(project, path, baseline, {'objects': []}, source_assignments=assignments)['ok'])
            group, = list_editions(project)['documents']
            self.assertEqual(group['name'], '文档A')
            self.assertEqual({v['versionLabel'] for v in group['versions']}, {'1', '2'})
            self.assertEqual(next(v for v in group['versions'] if v['isDefaultVersion'])['versionLabel'], '2')
            self.assertEqual({r['versionLabel'] for r in search_project_index(project, '创建订单')['results']}, {'2'})
            old = next(v for v in group['versions'] if v['versionLabel'] == '1')
            self.assertTrue(search_project_index(scope_project(project, {group['id']: old['editionId']}), '旧版独有')['results'])
            # Metadata survives rescanning, rebuilding and a repeat ingestion.
            rescanned = scan_ingestion_sources(project)
            self.assertEqual({item['documentName'] for item in rescanned['files']}, {'文档A'})
            again = [{'sourceFile': item['sourceFile'], 'logicalDocumentId': group['id'], 'versionLabel': item['versionLabel']} for item in rescanned['files']]
            self.assertTrue(validate_source_assignments(project, again))
            self.assertTrue(build_project_index(project)['ok'])
            self.assertEqual(list_editions(project)['documents'][0]['name'], '文档A')

    def test_invalid_assignment_is_rejected_before_extraction_and_publish_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self.make_project(directory)
            for invalid in (
                [{'sourceFile': '../outside.md', 'documentName': 'A', 'versionLabel': '1'}],
                [{'sourceFile': '文档A_1.md', 'logicalDocumentId': 'missing', 'versionLabel': '1'}],
                [{'sourceFile': 'missing.md', 'documentName': 'A', 'versionLabel': '1'}],
                [{**item, 'versionLabel': 'same'} for item in self.assignments()],
                [{**item, 'setDefault': True} for item in self.assignments()],
            ):
                with self.assertRaises(ValueError):
                    validate_source_assignments(project, invalid)
            with self.assertRaisesRegex(ValueError, '本次摄入范围'):
                validate_source_assignments(project, self.assignments(), ['文档A_1.md'])
            build_project_index(project)
            before = list_editions(project)
            staged, baseline = stage_project_import(project, Path(directory) / 'stage.db')
            with self.assertRaises(ValueError):
                publish_document_import(project, Path(directory) / 'stage.db', baseline, {'objects': []},
                    source_assignments=[{**item, 'versionLabel': 'duplicate'} for item in self.assignments()])
            self.assertEqual(list_editions(project), before)

    def test_new_version_can_join_an_existing_document_during_ingestion(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self.make_project(directory)
            build_project_index(project)
            group = next(g for g in list_editions(project)['documents'] if g['name'] == '文档A_1')
            assignments = [
                {'sourceFile': '文档A_1.md', 'logicalDocumentId': group['id'], 'versionLabel': '1'},
                {'sourceFile': '文档A_2.md', 'logicalDocumentId': group['id'], 'versionLabel': '2', 'setDefault': True},
            ]
            self.assertTrue(validate_source_assignments(project, assignments))
            staged, baseline = stage_project_import(project, Path(directory) / 'stage.db')
            publish_document_import(project, Path(directory) / 'stage.db', baseline, {'objects': []}, source_assignments=assignments)
            result, = list_editions(project)['documents']
            self.assertEqual(result['id'], group['id'])
            self.assertEqual(len(result['versions']), 2)

    def test_first_ingestion_of_selected_files_does_not_publish_unselected_files(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self.make_project(directory)
            staged, baseline = stage_project_import(project, Path(directory) / 'selected.db', ['文档A_1.md'])
            publish_document_import(project, Path(directory) / 'selected.db', baseline, {'objects': []},
                                    source_assignments=[self.assignments()[0]])
            group, = list_editions(project)['documents']
            self.assertEqual([v['sourceRef'] for v in group['versions']], ['文档A_1.md'])


class IngestionAssignmentAPITests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        raw = self.root / 'raw' / 'sources'
        raw.mkdir(parents=True)
        (raw / 'A.md').write_text('# 说明\n内容。', encoding='utf-8')
        self.user = get_user_model().objects.create_user('intake-admin', password='password')
        self.client.force_login(self.user)

    def test_scan_permissions_and_job_retry_preserves_user_choices(self):
        scan = lambda: self.client.post('/api/ingestion/sources/', json.dumps({'projectDir': str(self.root)}), content_type='application/json')
        self.assertEqual(scan().status_code, 403)
        self.user.is_staff = True
        self.user.save()
        self.assertEqual(scan().status_code, 200)
        self.assertEqual(scan().json()['files'][0]['sourceFile'], 'A.md')
        from api import views
        assignments = [{'sourceFile': 'A.md', 'documentName': '文档A', 'versionLabel': '1', 'setDefault': True}]
        with patch('api.views.threading.Thread'):
            job = views.start_ingestion_job({'sourceAssignments': assignments}, self.root)
        try:
            self.assertEqual(job['request']['sourceAssignments'][0]['documentName'], '文档A')
            views.release_ingestion_target(job['id'], job['exportDir'], job['projectDir'])
            job.update(status='failed', retryable=True)
            views.save_job(job)
            with patch('api.views.threading.Thread'):
                retried = self.client.post(f"/api/ingestion/jobs/{job['id']}/retry/", '{}', content_type='application/json')
            self.assertEqual(retried.status_code, 200)
            data = retried.json()
            self.assertEqual(data['request']['sourceAssignments'], job['request']['sourceAssignments'])
            views.release_ingestion_target(data['id'], data['exportDir'], data['projectDir'])
        finally:
            views.release_ingestion_target(job['id'], job['exportDir'], job['projectDir'])
