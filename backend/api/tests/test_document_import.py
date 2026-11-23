import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from api.search_index import (
    build_project_index, connect_index, get_ingestion_documents,
    stage_document_import, publish_document_import, verify_project_index,
)


class DocumentImportTests(SimpleTestCase):
    def payload(self, project, refs):
        mentions = []
        for doc in get_ingestion_documents(project, refs):
            chunk = doc['sections'][0]
            mentions.append({key: chunk[key] for key in (
                'chunkId', 'documentVersionId', 'structuralNodeId', 'startOffset', 'endOffset'
            )})
        return {'objects': [{'objectId': 'shared', 'name': '共享对象',
                             'sources': refs, 'mentions': mentions,
                             'relations': [{'type': '状态', 'value': '正常', 'evidence': mentions}]}]}

    def test_selected_replacement_preserves_other_document_and_shared_object(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / 'raw'
            raw.mkdir()
            for name in ('a.md', 'b.md'):
                (raw / name).write_text('# 共享对象\n旧内容 ' + name, encoding='utf-8')
            project = {'path': str(root), 'rawDir': str(raw)}
            build_project_index(project)
            build_project_index(project, extracted_knowledge=self.payload(project, ['a.md', 'b.md']))
            def snapshot():
                conn = connect_index(project)
                data = [tuple(row) for row in conn.execute('SELECT * FROM chunks ORDER BY id')]
                other_mentions = [tuple(row) for row in conn.execute("SELECT m.* FROM object_mentions m JOIN chunks c ON c.id=m.raw_chunk_id JOIN documents d ON d.id=c.document_id WHERE d.relative_path='b.md'")]
                conn.close()
                return data, other_mentions
            before = snapshot()
            (raw / 'a.md').write_text('# 共享对象\n新内容', encoding='utf-8')
            staged, baseline = stage_document_import(project, ['a.md'], root / 'stage.db')
            self.assertEqual(snapshot(), before)  # Failed/interrupted extraction never changes live data.
            result = publish_document_import(project, root / 'stage.db', baseline, self.payload(staged, ['a.md']))
            self.assertTrue(result['ok'])
            after = snapshot()
            self.assertEqual(after[1], before[1])
            self.assertTrue(any('新内容' in str(row) for row in after[0]))
            self.assertTrue(verify_project_index(project)['ok'])
            conn = connect_index(project)
            self.assertEqual(conn.execute('SELECT count(*) FROM knowledge_objects').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT count(*) FROM object_sources').fetchone()[0], 3)  # Both editions of a.md survive.
            self.assertEqual(conn.execute('SELECT count(*) FROM claim_evidence').fetchone()[0], 3)
            conn.close()
            with self.assertRaisesRegex(ValueError, '索引已更新'):
                publish_document_import(project, root / 'stage.db', baseline, self.payload(staged, ['a.md']))

    def test_invalid_selection_leaves_live_index_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = {'path': str(root), 'rawDir': str(root)}
            with self.assertRaises(ValueError):
                stage_document_import(project, ['../outside.md'], root / 'stage.db')
