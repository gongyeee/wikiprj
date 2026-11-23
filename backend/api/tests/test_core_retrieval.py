import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from api.query_roles import query_roles, meaningful_phrases
from api.search_index import build_project_index, search_project_index, get_ingestion_documents


class CoreRetrievalTests(SimpleTestCase):
    def test_compound_phrase_falls_back_to_object_not_attribute(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '交易.md').write_text('# 交易机制\n撮合按照价格优先执行。', encoding='utf-8')
            (root / '背景.md').write_text('# 规则\n文件管理规则与合规说明。', encoding='utf-8')
            project = {'path': str(root), 'rawDir': str(root)}
            build_project_index(project)
            result = search_project_index(project, '撮合规则')
            self.assertEqual(len(result['results']), 1)
            self.assertIn('撮合', result['results'][0]['snippet'])
            self.assertTrue(result['results'][0]['retrieval']['phraseFallback'])
            self.assertEqual(result['index']['queryRoles']['coreObjects'], ['撮合'])
            self.assertEqual(result['index']['queryRoles']['requestedAttribute'], '规则')
            self.assertEqual(search_project_index(project, '“撮合规则”')['results'], [])
            (root / '精确.md').write_text('# 撮合规则\n优先匹配完整短语。', encoding='utf-8')
            build_project_index(project)
            result = search_project_index(project, '撮合规则')
            self.assertFalse(result['results'][0]['retrieval']['phraseFallback'])
            self.assertEqual(result['results'][0]['title'], '精确')
            exact_only = search_project_index(project, '撮合规则', limit=1)
            self.assertFalse(exact_only['index']['phraseFallbackUsed'])

    def test_fragments_do_not_recall_background_or_link_descriptions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / 'raw'
            raw.mkdir()
            (raw / '需求.md').write_text('# 性能\n订单响应时延的基准条件是单机空载。\n# 产品\n对于产品而言，新增的基础数据如下。', encoding='utf-8')
            project = {'path': str(root), 'rawDir': str(raw)}
            build_project_index(project)
            chunks = get_ingestion_documents(project)[0]['sections']
            product = next(chunk for chunk in chunks if '新增' in chunk['text'])
            mention = {key: product[key] for key in ('chunkId', 'documentVersionId', 'structuralNodeId', 'startOffset', 'endOffset')}
            build_project_index(project, extracted_knowledge={'objects': [{
                'name': '产品', 'description': '对于产品而言，新增的基础数据。',
                'sources': ['需求.md'], 'mentions': [mention]}]})
            question = '技术指标而言，订单响应时延统计的基准条件是什么'
            self.assertEqual(meaningful_phrases(question), ['技术指标', '订单响应时延', '基准条件'])
            result = search_project_index(project, question)
            self.assertTrue(result['results'])
            self.assertTrue(all('新增' not in item['snippet'] for item in result['results']))
            self.assertIn({'term': '订单响应时延', 'location': '正文'}, result['results'][0]['retrieval']['matchLocations'])
            self.assertTrue(all(not item['retrieval']['indexTerms'] for item in result['results']))

    def test_disconnected_grams_are_not_a_phrase_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '碎片.md').write_text('# 例子\n订单响\n单响应\n响应时\n应时延', encoding='utf-8')
            project = {'path': str(root), 'rawDir': str(root)}
            build_project_index(project)
            result = search_project_index(project, '订单响应时延', roles=query_roles('订单响应时延', {'coreObjects': ['订单响应时延']}))
            self.assertEqual(result['results'], [])

    def test_core_body_and_ancestor_scope_outrank_background(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / 'raw'
            raw.mkdir()
            (raw / '新港股通.md').write_text(
                '# 概述\n新港股通 RC主机 文件格式 性能 配置\n'
                '# RC\n## 文件说明\napp_shell 使用每行一个键值对的格式。\n'
                '# 其他环境\napp_shell 使用其他格式。\n'
                '# 背景\napp_shell_backup 是另一个文件。\n', encoding='utf-8')
            project = {'path': str(root), 'rawDir': str(raw)}
            build_project_index(project)
            question = '新港股通中RC主机上的app_shell文件格式'
            result = search_project_index(project, question + ' 性能 配置 文件格式', roles=query_roles(question))
            self.assertTrue(result['results'])
            first = result['results'][0]
            self.assertIn('RC > 文件说明', first['headingPath'])
            self.assertEqual(first['retrieval']['coreHits'], ['app_shell'])
            self.assertEqual(first['retrieval']['scopeHits'], ['新港股通', 'RC主机'])
            self.assertFalse(any('概述' in item['headingPath'] or '背景' in item['headingPath'] for item in result['results']))
            missing = search_project_index(project, '新港股通 missing_file 文件格式', roles=query_roles('新港股通 missing_file 文件格式'))
            self.assertTrue(missing['results'])
            self.assertTrue(all('仅作为相关背景' in item['retrieval']['reason'] for item in missing['results']))

    def test_proposals_are_grounded_and_comparisons_keep_both_objects(self):
        roles = query_roles('app_shell和app_config的格式有什么区别', {'coreObjects': ['凭空对象']})
        self.assertEqual(roles['coreObjects'], ['app_shell', 'app_config'])
        self.assertEqual(roles['coreConfidence'], 'high')
        self.assertEqual(query_roles('RC如何配置', {'coreObjects': ['RC']})['coreObjects'], ['RC'])

    def test_comparison_reserves_results_for_each_object(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / 'raw'
            raw.mkdir()
            (raw / '配置.md').write_text(
                '# 一\napp_shell 格式\n# 二\napp_shell 格式\n# 三\napp_config 格式', encoding='utf-8')
            project = {'path': str(root), 'rawDir': str(raw)}
            build_project_index(project)
            result = search_project_index(project, 'app_shell和app_config格式区别', limit=2)
            hits = {term for item in result['results'] for term in item['retrieval']['coreHits']}
            self.assertEqual(hits, {'app_shell', 'app_config'})
