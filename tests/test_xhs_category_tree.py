import tempfile
import unittest
import asyncio
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

from attribute_runtime import AttributeRuntime, ReviewBatchRequired
from learning_store import LearningStore
from tests import test_xhs_form_listing as form_tests
from xhs_listing import XhsListing


TREE = [
    {'cid':377117237,'name':'男装','leaf':0,'children':[
        {'cid':377117318,'name':'针织衫/毛衣','leaf':0,'children':[
            {'cid':377117319,'name':'毛针织套头衫','leaf':1},
            {'cid':377117320,'name':'毛针织开衫','leaf':1},
        ]},
        {'cid':8,'name':'休闲裤','leaf':1},
    ]},
    {'cid':9,'name':'女装/女士精品','leaf':0,'children':[
        {'cid':10,'name':'羊毛衫','leaf':1},
    ]},
]


class XhsCategoryTreeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = form_tests.XhsFormListingTests.asyncSetUp
    asyncTearDown = form_tests.XhsFormListingTests.asyncTearDown
    _listing = form_tests.XhsFormListingTests._listing
    _ordered_category_listing = form_tests.XhsFormListingTests._ordered_category_listing
    async def tree_listing(self):
        await self.page.route('https://api.test/',lambda route:route.fulfill(body='<body></body>',content_type='text/html'))
        await self.page.goto('https://api.test/')
        listing = await self._ordered_category_listing({'毛衣':[['全新奢侈品','贸易男装/男士精品','针织衫/毛衣']]})
        await self.page.route('**/xhs/getCategoryTree.json*',lambda route:route.fulfill(json={'result':1,'data':TREE}))
        await self.page.evaluate('''tree => {
          window.tree=tree;
          window.openCategory=()=>{
            document.querySelector('.el-dialog').style.display='block';
            document.getElementById('chosen').textContent='已选：';
            const root=document.getElementById('nodes');root.innerHTML='<div class="el-cascader-panel"></div>';
            window.selectedNames=[];
            window.drawMenu=(rows,level)=>{
              const panel=root.querySelector('.el-cascader-panel');
              [...panel.children].slice(level).forEach(node=>node.remove());
              const menu=document.createElement('div');menu.className='el-cascader-menu';
              for(const item of rows){
                const node=document.createElement('div');node.className='el-cascader-node';node.textContent=item.name;
                node.onclick=()=>{
                  window.selectedNames=window.selectedNames.slice(0,level);window.selectedNames.push(item.name);
                  if(item.leaf===1)document.getElementById('chosen').textContent='已选：'+window.selectedNames.join(' > ');
                  else window.drawMenu(item.children,level+1);
                };menu.appendChild(node);
              }panel.appendChild(menu);
            };window.drawMenu(window.tree,0);
          };
        }''', TREE)
        return listing

    async def test_real_tree_leaf_is_found_even_when_keyword_search_omits_it(self):
        listing = await self.tree_listing()
        report = await listing.apply_category(('服装','男装','针织衫/毛衣/羊毛衫/毛针织开衫/开衫毛衣'))
        self.assertEqual(report['selected'],'男装 > 针织衫/毛衣 > 毛针织开衫')
        self.assertEqual(report['category_tree_id'],'377117320')
        self.assertEqual(self.searches,[])

    async def model_listing(self, directory, confidence):
        listing = await self.tree_listing()
        store=LearningStore(Path(directory)/'learning.sqlite3');store.migrate()
        client=Mock();client.post_event.return_value={'ok':True}
        client.review_learning.return_value={'status':'review_required'}
        client.decide.return_value={'status':'review_required'}
        def judge(payload):
            choices=[o for o in payload['options'] if o['label']=='男装 > 针织衫/毛衣 > 毛针织开衫']
            if len(choices)!=1: raise AssertionError('模型没有收到正确的树叶子候选')
            choice=choices[0]
            return {'status':'suggested','source':'deepseek','api_called':True,'snapshot_version':payload['snapshot_version'],
                'model':'deepseek-flash','value_ids':[choice['value_id']], 'confidence':confidence,
                'reason':'商品为男士开衫，选择男装针织衫下开衫叶子','evidence_kinds':['excel','product'],
                'assessment':{'relation':'inferred','source_text':payload['excel_value'],
                              'assumptions':[],'conflicts':[],'competing_value_ids':[]},
                'grounded_support':[{'source':'product','text':'男士开衫'}]}
        client.semantic_judge.side_effect=judge
        listing.attribute_runtime=AttributeRuntime(store,client,'run','product')
        listing.category_hints=('服装','男装','毛衣')
        return listing,store

    async def test_model_receives_sibling_tree_leaves_and_selects_existing_path(self):
        with tempfile.TemporaryDirectory() as directory:
            listing,store=await self.model_listing(directory,.99)
            try:
                report=await listing.apply_category(('服装','男装','毛衣'))
                await listing.attribute_runtime.drain()
                self.assertEqual(report['selected'],'男装 > 针织衫/毛衣 > 毛针织开衫')
                trace=listing.attribute_runtime.model_decisions[0]
                self.assertEqual(len(trace['candidate_options']),2)
                self.assertTrue(trace['input_evidence']['api_category_tree'])
            finally: store.close()

    async def test_confirmed_learning_is_reused_before_requesting_model(self):
        from learning_models import canonical_sha256
        with tempfile.TemporaryDirectory() as directory:
            listing, store = await self.model_listing(directory, .99)
            client = listing.attribute_runtime.client
            label = '男装 > 针织衫/毛衣 > 毛针织开衫'
            def learned(payload):
                return {'status':'auto_fill_ready', 'source':'review_learning',
                        'snapshot_version':payload['snapshot_version'],
                        'support_count':3, 'calibrated_acceptance_rate':1,
                        'value_ids':['path:' + canonical_sha256(listing._normalize_category_path(label))[:24]],
                        'value_labels':[label]}
            client.review_learning.side_effect = learned
            try:
                report = await listing.apply_category(('服装', '男装', '毛衣'))
                await listing.attribute_runtime.drain()
                self.assertEqual(report['selected'], label)
                client.semantic_judge.assert_not_called()
            finally: store.close()

    async def test_exact_excel_leaf_explains_model_was_not_called(self):
        with tempfile.TemporaryDirectory() as directory:
            listing, store = await self.model_listing(directory, .99)
            try:
                await listing.apply_category(('服装', '男装', '毛衣/毛针织开衫'))
                await listing.attribute_runtime.drain()
                listing.attribute_runtime.client.semantic_judge.assert_not_called()
                event = listing.attribute_runtime.model_events[0]
                self.assertFalse(event['api_called'])
                self.assertEqual(event['reason_code'], 'explicit_text')
                self.assertEqual(event['selected_labels'], ['男装 > 针织衫/毛衣 > 毛针织开衫'])
            finally: store.close()

    async def test_missing_search_response_advances_to_next_excel_leaf(self):
        listing = await self._ordered_category_listing({
            '毛针织开衫': [['男装', '针织衫/毛衣', '毛针织开衫']],
        })
        listing.category_search_timeout_seconds = .3
        await self.page.evaluate('''() => {
          const show = window.showRoot;
          window.showRoot = value => value === '毛衣' ? undefined : show(value);
        }''')
        report = await asyncio.wait_for(listing.apply_category(('服装', '男装', '毛衣/毛针织开衫')), timeout=5)
        self.assertEqual(report['selected'], '男装 > 针织衫/毛衣 > 毛针织开衫')

    async def test_tree_api_failure_falls_back_to_ordered_excel_search(self):
        listing = await self._ordered_category_listing({
            '休闲裤': [], '工装休闲裤': [['男装', '休闲裤', '工装休闲裤']],
        })
        await self.page.route('**/xhs/getCategoryTree.json*', lambda route: route.fulfill(status=503, json={'result':0}))
        report = await listing.apply_category(('休闲裤/工装休闲裤',))
        self.assertEqual(report['selected'], '男装 > 休闲裤 > 工装休闲裤')
        self.assertEqual(self.searches, ['休闲裤', '工装休闲裤'])

    async def test_cascader_clicks_scoped_level_and_preserves_slash_label_and_gray_badge(self):
        listing = await self.tree_listing()
        await self.page.evaluate('''() => {
          const open = window.openCategory;
          window.openCategory = () => {
            open();
            const draw = window.drawMenu;
            window.drawMenu = (rows, level) => {
              draw(rows, level);
              const menu=document.querySelectorAll('.el-cascader-menu')[level];
              for (const node of menu.children) node.textContent += '(灰度)';
            };
            const decoy=document.createElement('div');decoy.className='el-cascader-node';
            decoy.textContent='毛针织开衫';document.getElementById('nodes').appendChild(decoy);
          };
        }''')
        report = await listing.apply_category(('服装', '男装', '毛针织开衫'))
        self.assertEqual(report['selected'], '男装 > 针织衫/毛衣 > 毛针织开衫')


    async def test_uncertain_tree_choice_goes_to_review_before_category_click(self):
        with tempfile.TemporaryDirectory() as directory:
            listing,store=await self.model_listing(directory,.6)
            listing.attribute_runtime.begin_review_collection('xhs')
            try:
                with self.assertRaises(ReviewBatchRequired) as caught:
                    await listing.apply_category(('服装','男装','毛衣'))
                await listing.attribute_runtime.drain()
                candidates=caught.exception.reviews[0].request.candidates
                self.assertEqual({c.label for c in candidates},{'男装 > 针织衫/毛衣 > 毛针织套头衫','男装 > 针织衫/毛衣 > 毛针织开衫'})
                self.assertEqual(await self.page.locator('#chosen').inner_text(),'已选：')
            finally: store.close()


class XhsTreeParsingTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_parent_is_not_a_leaf_and_does_not_refetch_whole_tree(self):
        tree = deepcopy(TREE)
        tree.append({'cid':30, 'name':'空分支', 'leaf':0, 'children':None})
        calls = []
        async def transport(endpoint, parameters):
            calls.append(parameters['parentId'])
            return {'result':1, 'data':tree}
        leaves = await XhsListing(transport=transport).read_category_tree()
        self.assertEqual(calls, ['0'])
        self.assertEqual({c.leaf_id for c in leaves}, {'377117319','377117320','8','10'})

    async def test_disabled_leaf_and_disabled_parent_are_not_selectable(self):
        tree = deepcopy(TREE)
        tree[0]['children'][0]['children'][0]['state'] = 0
        tree[1]['disabled'] = True
        async def transport(endpoint, parameters):
            return {'result':1, 'data':tree}
        leaves = await XhsListing(transport=transport).read_category_tree()
        self.assertEqual({c.leaf_id for c in leaves}, {'377117320','8'})
