"""Exact-ID context preserves corrections, source bytes and page boundaries."""
import copy
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import rivet


def fixture():
    def rec(ident,claim,kind='decision'):
        return {'id':ident,'claim':claim,'kind':kind,'topic':'cache','title':ident,
                'actor':'author','authority':'reported_agent','state':'recorded',
                'recorded_at':'2025-01-01','sources':[{'path':'notes.md','start_line':1,
                'end_line':1,'sha256':'file-digest','excerpt':'Original observed evidence.'}]}
    records=[rec('old','Use memory provisionally.'),rec('new','Use bounded memory and disk.'),
             rec('approval','Human approved only the original trial.'),
             rec('proof','Load exceeded the memory limit.','evidence')]
    records[2]['authority']='reported_human'
    edges=[{'from':'new','to':'old','relation':'supersedes','note':'Later correction','sources':[]},
           {'from':'approval','to':'old','relation':'affirms','note':'Trial only','sources':[]},
           {'from':'proof','to':'new','relation':'supports','note':'Measured limit exceeded','sources':[]}]
    return {'schema':'decision-store.v1','records':records,'edges':edges}


class RivetTests(unittest.TestCase):
    def test_current_first_and_approval_stays_on_old(self):
        r=rivet.receipt(fixture(),['old'])
        self.assertEqual('new',r['records'][0]['id'])
        self.assertEqual(['new'],r['tips']['old']['recorded_tips'])
        self.assertEqual('old',next(e['to'] for e in r['relationships'] if e['from']=='approval'))
        self.assertNotIn('Original observed evidence.',rivet.render(r))

    def test_evidence_uses_capture_without_live_path(self):
        store=fixture();r=rivet.receipt(store,['old'])
        sid=next(iter(r['sources']))
        evidence,text=rivet.evidence(store,sid)
        self.assertEqual('captured',evidence['status'])
        self.assertIn('Original observed evidence.',text)
        self.assertIn('no live file was read',text)
        self.assertIn('Original observed evidence.',rivet.render(rivet.receipt(store,['old'],sources=True)))

    def test_pagination_and_followup_cli_keep_every_claim_and_edge(self):
        store=fixture()
        with tempfile.TemporaryDirectory(prefix='rivet-page-') as tmp:
            path=Path(tmp)/'a store.json';path.write_text(json.dumps(store))
            command=[sys.executable,str(Path(rivet.__file__)), '--store',str(path), 'context','old','--sources','--format','json','--budget','2200','--count','2']
            cards=[];edges=[];pages=0
            while command:
                raw=subprocess.check_output(command,text=True)
                self.assertLessEqual(len(raw.encode()),2200)
                r=json.loads(raw);cards+=r['records'];edges+=r['relationships'];pages+=1
                if r['follow_up']:
                    command=shlex.split(r['follow_up']);command[:2]=[sys.executable,str(Path(rivet.__file__))]
                else:command=None
            self.assertGreater(pages,1)
            self.assertEqual(4,len(cards));self.assertEqual(4,len({r['id'] for r in cards}))
            self.assertEqual(3,len(edges));self.assertEqual(3,len({(e['from'],e['relation'],e['to']) for e in edges}))
            self.assertTrue(all('excerpt' in s for s in r['sources'].values()))

    def test_fork_and_cycle_remain_ambiguous(self):
        store=fixture();other=copy.deepcopy(store['records'][1]);other['id']='other';store['records'].append(other)
        store['edges'].append({'from':'other','to':'old','relation':'supersedes','note':'conflict','sources':[]})
        self.assertTrue(rivet.receipt(store,['old'])['tips']['old']['ambiguous'])
        store['edges'].append({'from':'old','to':'new','relation':'supersedes','note':'cycle','sources':[]})
        self.assertTrue(rivet.receipt(store,['old'])['tips']['old']['cycle'])

    def test_next_page_can_expand_after_a_large_first_record(self):
        store=fixture()
        store['records'][1]['claim']='A long complete decision. '*160
        # One large record fits; adding its next neighbor exceeds this byte cap.
        _,one=rivet.bounded(store,['old'],100000,count=1,fmt='json',sources=True)
        budget=len(one.encode())+64
        for requested in (2,20):
            offset=0;count=requested;pages=[];cards=[];edges=[]
            while True:
                packet,text=rivet.bounded(store,['old'],budget,offset=offset,
                                          count=count,fmt='json',sources=True)
                self.assertNotIn('error',packet)
                self.assertLessEqual(len(text.encode()),budget)
                pages.append(packet);cards+=packet['records'];edges+=packet['relationships']
                self.assertLessEqual(len(pages),4,'Pagination must terminate')
                if not packet['follow_up']:break
                command=shlex.split(packet['follow_up'])
                count=int(command[command.index('--count')+1])
                offset=int(command[command.index('--offset')+1])
                self.assertEqual(requested,count,'NEXT must preserve the requested count')
            self.assertEqual(1,len(pages[0]['records']))
            self.assertEqual(min(requested,3),len(pages[1]['records']))
            self.assertEqual({'old','new','approval','proof'},{r['id'] for r in cards})
            self.assertEqual(4,len(cards))
            self.assertEqual(3,len(edges))
            self.assertEqual(3,len({(e['from'],e['relation'],e['to']) for e in edges}))

    def test_unknown_record_or_source_is_not_empty_success(self):
        with self.assertRaisesRegex(ValueError,'unknown entry'):rivet.receipt(fixture(),['missing'])
        with self.assertRaisesRegex(ValueError,'unknown evidence'):rivet.evidence(fixture(),'missing')

    def test_missing_excerpt_and_conflicting_sources_are_explicit(self):
        store=fixture()
        for r in store['records']:r['sources'][0].pop('excerpt')
        sid=next(iter(rivet.source_index(store)))
        self.assertEqual('unavailable',rivet.evidence(store,sid)[0]['status'])
        store['records'][0]['sources'][0]['excerpt']='Different bytes'
        with self.assertRaisesRegex(ValueError,'conflicting'):rivet.source_index(store)

    def test_budget_never_cuts_claim_or_evidence(self):
        store=fixture();store['records'][1]['claim']='x'*5000
        result,text=rivet.bounded(store,['old'],300)
        self.assertIn('error',result);self.assertLessEqual(len(text.encode()),300)
        store=fixture()
        for r in store['records']:r['sources'][0]['excerpt']='x'*5000
        result,text=rivet.evidence(store,next(iter(rivet.source_index(store))),300)
        self.assertEqual('evidence_exceeds_budget',result['error']);self.assertLessEqual(len(text.encode()),300)

    def test_find_evidence_deduplicates_spans_and_exposes_ownership(self):
        store=fixture();store['records'][0]['state']='superseded'
        result,text=rivet.find_evidence(store,'Original observed')
        self.assertEqual(1,result['total_matches'])
        self.assertEqual(4,len(result['matches'][0]['record_refs']))
        self.assertIn({'id':'old','state':'superseded'},result['matches'][0]['record_refs'])
        self.assertEqual('Original observed evidence.',result['matches'][0]['excerpt'])
        self.assertIn('not a current-policy verdict',text)
        self.assertIn('context',result['matches'][0]['context'])
        self.assertEqual(0,rivet.find_evidence(store,'original observed')[0]['total_matches'])

    def test_find_evidence_reports_unavailable_capture_and_blank_query(self):
        store=fixture()
        for record in store['records']:record['sources'][0].pop('excerpt')
        result,text=rivet.find_evidence(store,'unobserved receipt')
        self.assertEqual(0,result['total_matches'])
        self.assertEqual(1,result['unavailable_spans'])
        self.assertFalse(result['captured_text_fully_searchable'])
        self.assertIn('No match does not establish absence',text)
        with self.assertRaisesRegex(ValueError,'blank'):rivet.find_evidence(store,'  ')

    def test_find_evidence_cli_pages_whole_matches_and_quotes_query(self):
        store=fixture();query="Receipt O'Reilly; literal"
        for number,record in enumerate(store['records'],1):
            record['sources'][0].update(start_line=number,end_line=number,
                                        excerpt=query+' '+record['id'])
        with tempfile.TemporaryDirectory(prefix='rivet-find-') as tmp:
            path=Path(tmp)/'store.json';path.write_text(json.dumps(store))
            command=[sys.executable,str(Path(rivet.__file__)),'--store',str(path),
                     'find-evidence',query,'--count','2','--format','json','--budget','6000']
            matches=[];pages=0
            while command:
                raw=subprocess.check_output(command,text=True)
                self.assertLessEqual(len(raw.encode()),6000)
                packet=json.loads(raw);matches+=packet['matches'];pages+=1
                self.assertLessEqual(pages,4)
                if packet['follow_up']:
                    command=shlex.split(packet['follow_up'])
                    self.assertIn(query,command)
                    command[:2]=[sys.executable,str(Path(rivet.__file__))]
                else:command=None
            self.assertEqual(4,len(matches));self.assertEqual(4,len({m['id'] for m in matches}))
            self.assertEqual(2,pages)
        result,text=rivet.find_evidence(store,query,budget=300)
        self.assertIn('error',result)
        self.assertNotIn('matches',result)
        self.assertLessEqual(len(text.encode()),300)


if __name__=='__main__':unittest.main()
