import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from api.document_versions import list_editions, update_edition, scope_project
from api.search_index import (
    build_project_index, connect_index, get_ingestion_documents,
    search_project_index, get_indexed_section_evidence, get_project_graph,
    stage_document_import, stage_project_import, publish_document_import, verify_project_index,
)
from api.views import prepare_compose


class VersionTests(SimpleTestCase):
    def make_project(self, directory):
        root = Path(directory)
        raw = root / 'raw'
        raw.mkdir()
        (raw / '接口_0314.md').write_text('# 创建订单\n字段A必填。旧版专用规则。', encoding='utf-8')
        (raw / '接口_0918.md').write_text('# 创建订单\n字段A选填。新增字段B。', encoding='utf-8')
        project = {'path': str(root), 'rawDir': str(raw)}
        self.assertTrue(build_project_index(project)['ok'])
        groups = list_editions(project)['documents']
        old = next(g for g in groups if g['name'] == '接口_0314')
        new = next(g for g in groups if g['name'] == '接口_0918')
        update_edition(project, {'editionId': old['versions'][0]['editionId'], 'versionLabel': '0314', 'documentName': '接口文档'})
        update_edition(project, {'editionId': new['versions'][0]['editionId'], 'versionLabel': '0918',
                                'logicalDocumentId': old['id'], 'setDefault': True, 'documentName': '接口文档'})
        return project, old['id']

    def test_default_history_evidence_compose_and_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            project, group_id = self.make_project(directory)
            group = list_editions(project)['documents'][0]
            old = next(v for v in group['versions'] if v['versionLabel'] == '0314')
            default = search_project_index(project, '创建订单')['results']
            self.assertTrue(default)
            self.assertEqual({row['versionLabel'] for row in default}, {'0918'})
            history_project = scope_project(project, {group_id: old['editionId']})
            history = search_project_index(history_project, '创建订单')['results']
            self.assertEqual({row['versionLabel'] for row in history}, {'0314'})
            self.assertEqual(search_project_index(project, '旧版专用规则')['results'], [])
            self.assertIn('旧版专用规则', get_indexed_section_evidence(history_project, [history[0]['chunkId']])[0]['content'])
            self.assertEqual(get_indexed_section_evidence(project, [history[0]['chunkId']]), [])
            with patch('api.views.get_project', return_value=project), patch('api.views.build_query_enrichment', return_value={
                    'searchQuery': '创建订单', 'queryIntent': {}, 'synonymExpansion': {}, 'expandedTerms': []}):
                with self.assertRaisesRegex(ValueError, '当前版本'):
                    prepare_compose('p', {'chunkIds': [history[0]['chunkId']]}, '创建订单')
                with self.assertRaisesRegex(ValueError, '当前版本'):
                    prepare_compose('p', {'paths': [old['path']]}, '创建订单')
                prepared = prepare_compose('p', {'chunkIds': [history[0]['chunkId']], 'versionScope': {group_id: old['editionId']}}, '创建订单')
                self.assertEqual(prepared['documents'][0]['versionLabel'], '0314')
                self.assertIn('版本：0314', prepared['llmDocuments'][0]['content'])
            # Removing live files and rebuilding the entire index uses durable snapshots.
            for path in Path(project['rawDir']).glob('*.md'):
                path.unlink()
            database = Path(directory) / '.index' / 'search.db'
            database.unlink()
            self.assertTrue(build_project_index(project, force=True)['ok'])
            self.assertEqual(list_editions(project)['documents'][0]['id'], group_id)
            self.assertEqual({row['versionLabel'] for row in search_project_index(project, '创建订单')['results']}, {'0918'})
            self.assertTrue(search_project_index(history_project, '旧版专用规则')['results'])

    def test_changed_file_is_a_new_edition_and_failed_publish_is_invisible(self):
        with tempfile.TemporaryDirectory() as directory:
            project, group_id = self.make_project(directory)
            raw = Path(project['rawDir']) / '接口_0918.md'
            raw.write_text('# 创建订单\n字段A禁止传入。', encoding='utf-8')
            before = list_editions(project)
            stage_path = Path(directory) / 'stage.db'
            staged, baseline = stage_document_import(project, ['接口_0918.md'], stage_path)
            self.assertEqual(list_editions(project), before)
            new_docs = get_ingestion_documents(staged, ['接口_0918.md'])
            self.assertEqual(len(new_docs), 1)
            self.assertIn('禁止', new_docs[0]['sections'][0]['text'])
            raw.write_text('# 创建订单\n再次修改。', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '原文已修改'):
                publish_document_import(project, stage_path, baseline, {'objects': []})
            self.assertEqual(list_editions(project), before)
            staged, baseline = stage_document_import(project, ['接口_0918.md'], stage_path)
            self.assertTrue(publish_document_import(project, stage_path, baseline, {'objects': []})['ok'])
            versions = list_editions(project)['documents'][0]['versions']
            self.assertEqual(len(versions), 3)
            self.assertEqual(next(v for v in versions if v['isDefaultVersion'])['versionLabel'], '0918')
            self.assertTrue(build_project_index(project)['ok'])
            self.assertEqual(len(list_editions(project)['documents'][0]['versions']), 3)
            self.assertTrue(verify_project_index(project)['ok'])

    def test_alias_description_and_claim_provenance_are_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            project, group_id = self.make_project(directory)
            objects = []
            for doc in get_ingestion_documents(project):
                old = '0314' in doc['sourceRef']
                pointer = {key: doc['sections'][0][key] for key in ('chunkId', 'documentVersionId', 'structuralNodeId')}
                objects.append({'objectId': 'shared', 'name': '创建订单', 'aliases': ['过时别名' if old else '新版别名'],
                    'description': '历史特殊描述' if old else '当前特殊描述', 'sources': [doc['sourceRef']],
                    'mentions': [pointer], 'relations': [{'type': '字段A', 'value': '必填' if old else '选填', 'evidence': [pointer]}]})
            self.assertTrue(build_project_index(project, extracted_knowledge={'objects': objects})['ok'])
            scoped = connect_index(scope_project(project))
            try:
                self.assertNotIn('过时别名', [r[0] for r in scoped.execute('SELECT term FROM object_terms')])
                self.assertIn('新版别名', [r[0] for r in scoped.execute('SELECT term FROM object_terms')])
                descriptions = ' '.join(r[0] for r in scoped.execute('SELECT description FROM knowledge_objects'))
                self.assertIn('当前特殊描述', descriptions)
                self.assertNotIn('历史特殊描述', descriptions)
                self.assertEqual([r[0] for r in scoped.execute('SELECT literal_value FROM claims')], ['选填'])
            finally:
                scoped.close()
            graph = get_project_graph(project)
            self.assertNotIn('必填', [node['label'] for node in graph['nodes']])
            self.assertIn('选填', [node['label'] for node in graph['nodes']])

    def test_invalid_scope_duplicate_labels_and_missing_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            project, group_id = self.make_project(directory)
            versions = list_editions(project)['documents'][0]['versions']
            with self.assertRaisesRegex(ValueError, '不能重复'):
                update_edition(project, {'editionId': versions[0]['editionId'], 'versionLabel': versions[1]['versionLabel']})
            for scope in ({'missing': 'missing'}, {group_id: 'missing'}):
                with self.assertRaises(ValueError):
                    search_project_index(scope_project(project, scope), '创建订单')
            with self.assertRaises(ValueError):
                scope_project(project, [])
            snapshot = next((Path(directory) / 'document-history').glob('*.md'))
            snapshot.unlink()
            with self.assertRaisesRegex(ValueError, '快照缺失'):
                build_project_index(project)

    def test_full_ingestion_stays_private_until_publish_and_then_selected_import_merges(self):
        with tempfile.TemporaryDirectory() as directory:
            project, group_id = self.make_project(directory)
            before = list_editions(project)
            (Path(project['rawDir']) / '接口_0918.md').write_text('# 创建订单\n第三版。', encoding='utf-8')
            path = Path(directory) / 'full.db'
            staged, baseline = stage_project_import(project, path)
            self.assertEqual(list_editions(project), before)
            self.assertEqual(len(get_ingestion_documents(staged)), 3)
            self.assertTrue(publish_document_import(project, path, baseline, {'objects': []})['ok'])
            # A later selected import must not inherit full-import replacement mode.
            staged, baseline = stage_document_import(project, ['接口_0918.md'], Path(directory) / 'selected.db')
            from api.search_index import get_meta
            connection = connect_index(staged)
            try:
                self.assertFalse(get_meta(connection, 'document_import_full'))
            finally:
                connection.close()
            self.assertTrue(publish_document_import(project, Path(directory) / 'selected.db', baseline, {'objects': []})['ok'])
            self.assertEqual(len(list_editions(project)['documents'][0]['versions']), 3)

    def test_migration_preserves_cached_text_when_legacy_source_was_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            project, group_id = self.make_project(directory)
            connection = connect_index(project, create=True)
            try:
                with connection:
                    connection.execute('DROP TABLE document_editions')
                    connection.execute("UPDATE documents SET absolute_path=? WHERE relative_path='接口_0314.md'", (str(Path(project['rawDir']) / '接口_0314.md'),))
            finally:
                connection.close()
            (Path(directory) / 'document-history' / 'catalog.json').unlink()
            (Path(project['rawDir']) / '接口_0314.md').write_text('# 创建订单\n覆盖后的内容。', encoding='utf-8')
            self.assertTrue(build_project_index(project)['ok'])
            self.assertTrue((Path(directory) / '.index' / 'search.db.pre-versions.bak').exists())
            groups = list_editions(project)['documents']
            old_group = next(g for g in groups if g['name'] == '接口_0314')
            self.assertEqual(len(old_group['versions']), 2)
            self.assertIn('仅文本', next(v for v in old_group['versions'] if v['isDefaultVersion'])['versionLabel'])
            self.assertTrue(search_project_index(project, '旧版专用规则')['results'])


class VersionAPITests(TestCase):
    def setUp(self):
        from api.models import Project, ProjectSourceRoot
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        raw = root / 'raw'
        raw.mkdir()
        (raw / '接口.md').write_text('# 创建订单\n字段A必填。', encoding='utf-8')
        project = Project.objects.create(name='版本测试', root_path=str(root))
        ProjectSourceRoot.objects.create(project=project, path=str(raw), is_primary=True)
        from api.projects import get_project
        self.project = get_project(str(project.pk))
        build_project_index(self.project)
        self.url = f'/api/projects/{project.pk}'
        self.user = get_user_model().objects.create_user('reader', password='password')
        self.client.force_login(self.user)

    def test_permissions_validation_and_stale_search_context(self):
        response = self.client.get(self.url + '/document-versions/')
        self.assertEqual(response.status_code, 200)
        old = response.json()['documents'][0]['versions'][0]
        post = lambda path, body: self.client.post(self.url + path, json.dumps(body), content_type='application/json')
        self.assertEqual(post('/document-versions/', {'editionId': old['editionId'], 'versionLabel': '0314'}).status_code, 403)
        self.user.is_staff = True
        self.user.save()
        self.assertEqual(post('/document-versions/', {'editionId': old['editionId'], 'publishedAt': 'invalid'}).status_code, 400)
        search = post('/search/', {'query': '创建订单', 'useIntent': False, 'useSynonyms': False}).json()
        raw = Path(self.project['rawDir']) / '接口.md'
        raw.write_text('# 创建订单\n字段A选填。', encoding='utf-8')
        build_project_index(self.project)
        new = list_editions(self.project)['documents'][0]['versions'][-1]
        update_edition(self.project, {'editionId': new['editionId'], 'setDefault': True})
        response = post('/compose/', {'query': '创建订单', 'searchContextId': search['searchContextId']})
        self.assertEqual(response.status_code, 400)
        self.assertIn('默认版本已变化', response.json()['error'])
        preview = self.client.get(self.url + '/file-content/', {'path': old['path'], 'editionId': old['editionId']})
        self.assertEqual(preview.status_code, 200)
        self.assertIn('必填', preview.json()['content'])
        blocked = self.client.get(self.url + '/file-content/', {'path': old['path']})
        self.assertEqual(blocked.status_code, 400)
