"""T09批量科目查询；只有已确认项能填入目标编码和名称。"""
import copy
from collections import defaultdict

from .configuration import ConfigurationError, compile_config
from .applicability import applies_to


class MappingIndex:
    def __init__(self,config):
        active,_=compile_config(config)
        self.confirmed={m['source_code']:copy.deepcopy(m) for m in active['mappings']}
        self.candidates=defaultdict(list);self.rejected=defaultdict(list)
        for mapping in config['mappings']:
            if mapping['status']=='candidate':self.candidates[mapping['source_code']].append(copy.deepcopy(mapping))
            elif mapping['status']=='rejected':self.rejected[mapping['source_code']].append(copy.deepcopy(mapping))

    def resolve(self,source_code,*,period=None,source_sha256=None):
        if not isinstance(source_code,str):raise ConfigurationError('外部科目必须为文本编码')
        approved=self.confirmed.get(source_code)
        candidates=self.candidates.get(source_code,[]);rejected=self.rejected.get(source_code,[])
        result={'source_code':source_code,'status':'unknown','target_code':None,'target_name':None,'mapping_id':None,
            'candidate_mapping_ids':[m['id'] for m in candidates],'rejected_mapping_ids':[m['id'] for m in rejected]}
        if approved and not applies_to(approved,period=period,source_sha256=source_sha256):
            result.update(status='out_of_scope',out_of_scope_mapping_ids=[approved['id']])
        elif approved:
            result.update(status='matched',target_code=approved['target_code'],target_name=approved['target_name'],mapping_id=approved['id'])
        elif candidates:
            result['status']='candidate_conflict' if len({(m['target_code'],m['target_name']) for m in candidates})>1 else 'unconfirmed'
        elif rejected:result['status']='rejected'
        return result


def not_evaluated(reason):
    return {'status':'not_evaluated','source_code':None,'target_code':None,'target_name':None,'mapping_id':None,
        'candidate_mapping_ids':[],'rejected_mapping_ids':[],'reason':reason}
