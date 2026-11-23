import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from api.query_roles import query_roles, segment_question
from api.search_index import build_project_index, search_project_index


class QuerySegmentationTests(SimpleTestCase):
    def test_dictionary_isolation_and_identifier_protection(self):
        protected = query_roles('撮合规则', domain_terms=['撮合规则'])
        self.assertIsNone(protected['compoundFallback'])
        self.assertEqual(protected['coreObjects'], ['撮合规则'])
        ordinary = query_roles('撮合规则', domain_terms=[])
        self.assertEqual(ordinary['compoundFallback']['object'], '撮合')
        tokens = segment_question('新港股通中的app_shell文件', ['新港股通'])
        self.assertIn('新港股通', [token['text'] for token in tokens if token['protected']])
        self.assertIn('app_shell', [token['text'] for token in tokens if token['protected']])
        self.assertIsNone(query_roles('“撮合规则”')['compoundFallback'])

    def test_jieba_joint_fallback_requires_all_words(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '联合.md').write_text('# 说明\n交易系统采用撮合处理，机制如下。', encoding='utf-8')
            (root / '泛词.md').write_text('# 背景\n交易信息与其他机制。', encoding='utf-8')
            project = {'path': str(root), 'rawDir': str(root)}
            build_project_index(project)
            result = search_project_index(project, '交易撮合机制')
            self.assertEqual([item['title'] for item in result['results']], ['联合'])
            self.assertTrue(result['index']['tokenFallbackUsed'])
            self.assertEqual(result['index']['queryRoles']['tokenizer'], 'jieba+domain')

    def test_project_dictionary_is_loaded_and_refreshed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '规则.md').write_text('# 内容\n撮合按照价格优先执行。', encoding='utf-8')
            project = {'path': str(root), 'rawDir': str(root)}
            build_project_index(project)
            self.assertTrue(search_project_index(project, '撮合规则')['results'])
            build_project_index(project, extracted_knowledge={'objects': [{'name': '撮合规则', 'sources': ['规则.md']}]})
            roles = search_project_index(project, '撮合规则')['index']['queryRoles']
            self.assertEqual(roles['coreObjects'], ['撮合规则'])
            self.assertIsNone(roles['compoundFallback'])
            self.assertGreater(roles['domainTermCount'], 0)
