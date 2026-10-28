"""统一转换产物的文件协议与清单；所有业务文件依赖有效运行清单。"""
import csv
import hashlib
import json
import re


def write_json(path,value):
    with path.open('x',encoding='utf-8') as stream:json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)


def write_bytes(path,value):
    with path.open('xb') as stream:stream.write(value)


def write_csv(path,rows,fields):
    def cell(value):
        if isinstance(value,(dict,list)):value=json.dumps(value,ensure_ascii=False)
        if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')) and not re.fullmatch(r'[+-]?[0-9]+(?:\.[0-9]+)?',value.strip()):
            return "'"+value
        return value
    with path.open('x',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
        for row in rows:writer.writerow({k:cell(v) for k,v in row.items()})


def stage_snapshot(output,stage,report,run_id):
    value={k:v for k,v in report.items() if k!='rows'}
    value.update(run_id=run_id,row_record_ids=[r['record_id'] for r in report.get('rows',[])])
    write_json(output/('stage_'+stage+'.json'),value)


def envelope(kind,rows,run_id,available=True,diagnostic=False):
    return {'schema_version':'0.2','kind':kind,'run_id':run_id,'requires_valid_manifest':True,
        'ready_for_system_import':False,'diagnostic_only':diagnostic,'source_data_available':available,'row_count':len(rows),'rows':rows}


def _flat(entry,run_id):
    return {'run_id':run_id,'record_id':entry['record_id'],'sheet':entry['provenance']['sheet'],
        'excel_row':entry['provenance']['excel_row'],'period':entry['period'],'voucher_category':entry['voucher_category'],
        'voucher_number':entry['voucher_number'],'entry_sequence':entry['entry_sequence'],'posting_date':entry['posting_date'],
        'original_date':entry['original_date'],'summary':entry['summary'],'external_subject_code':entry['external_subject_code'],
        'target_subject_code':entry['target_subject_code'],'target_subject_name':entry['target_subject_name'],
        'debit':entry['debit']['value'],'debit_status':entry['debit']['status'],'credit':entry['credit']['value'],'credit_status':entry['credit']['status'],
        'debit_raw':entry['debit'].get('raw'),'credit_raw':entry['credit'].get('raw'),'raw_subject':entry['raw_subject'],
        'mapping_status':entry['mapping_status'],'decision':entry['decision'],'reason_codes':entry['reason_codes'],
        'diagnostic_only':entry['diagnostic_only'],'ready_for_system_import':False}


def write_exports(output,bundle,issues,stages,config,run_id):
    available=bundle['source_data_available']
    fields=['run_id','record_id','sheet','excel_row','period','voucher_category','voucher_number','entry_sequence','posting_date','original_date','summary','external_subject_code','target_subject_code','target_subject_name','debit','debit_status','credit','credit_status','debit_raw','credit_raw','raw_subject','mapping_status','decision','reason_codes','diagnostic_only','ready_for_system_import']
    for name,key in [('candidates','candidates'),('pending_rows','pending'),('excluded_rows','excluded'),('diagnostic_candidates','diagnostic_candidates')]:
        rows=bundle[key]
        payload=envelope(name,rows,run_id,available,name=='diagnostic_candidates')
        payload['partial_export']=bundle.get('batch_gate',{}).get('partial_export',False)
        write_json(output/(name+'.json'),payload)
        write_csv(output/(name+'.csv'),[_flat(r,run_id) for r in rows],fields)
    write_json(output/'source_rows.json',envelope('source_rows',bundle['source_rows'],run_id,available))
    if config and config.get('auxiliary_profile'):
        proposals=[{'record_id':row['record_id'],'sheet':row['provenance']['sheet'],'excel_row':row['provenance']['excel_row'],
                    'voucher_number':(row.get('voucher') or {}).get('number'),'target_code':row.get('account_mapping',{}).get('target_code'),**value}
                   for row in bundle['source_rows'] for value in row.get('auxiliary_candidates',{}).values()]
        write_json(output/'auxiliary_candidates.json',envelope('auxiliary_candidates',proposals,run_id,available,True))
        labels={'customer':'客户','supplier':'供应商','employee':'员工','department':'部门','project':'项目','stock_name':'存货','customer_level':'客户等级','inbound_type':'其他入库类别','outbound_type':'其他出库类别','subject_name':'原始科目名称'}
        source_label=lambda sf:labels.get(sf[:-5],sf[:-5])+'编码' if sf.endswith('_code') else labels.get(sf,sf)
        write_csv(output/'辅助核算取值核验.csv',[{'原始行号':r['excel_row'],'凭证号':r['voucher_number'],'目标科目':r['target_code'],
            '字段':labels.get(r['field'],r['field']),'来源字段':source_label(r['source_field']),'原始来源值':r['source_value'],
            '提取名称':r['value'],'备查匹配':{'matched':'唯一匹配','missing':'档案缺失','ambiguous':'多项歧义'}[r['dictionary_status']],
            '备查编码':r['dictionary_code'],'已批准取值':'是' if r['approved'] else '否'} for r in proposals],
            ['原始行号','凭证号','目标科目','字段','来源字段','原始来源值','提取名称','备查匹配','备查编码','已批准取值'])
    write_json(output/'row_decisions.json',envelope('row_decisions',bundle['row_decisions'],run_id,available))
    write_csv(output/'row_decisions.csv',[{'record_id':r['record_id'],'sheet':r['provenance']['sheet'],'excel_row':r['provenance']['excel_row'],
        'decision':r['decision'],'reason_codes':r['reason_codes'],'exception_ids':r['exception_ids'],'rule_ids':r['rule_ids'],'stage_decisions':r['stage_decisions']} for r in bundle['row_decisions']],
        ['record_id','sheet','excel_row','decision','reason_codes','exception_ids','rule_ids','stage_decisions'])
    write_json(output/'exceptions.json',{'schema_version':'0.2','run_id':run_id,'requires_valid_manifest':True,
        'issue_count':len(issues),'affected_record_count':len({i for e in issues for i in e['affected_record_ids']}),'issues':issues})
    write_csv(output/'exceptions.csv',[{k:e[k] for k in ['issue_id','code','scope','severity','message','locator','source_record_ids','affected_record_ids','provenance','rule_ids','evidence_ids','details']} for e in issues],
        ['issue_id','code','scope','severity','message','locator','source_record_ids','affected_record_ids','provenance','rule_ids','evidence_ids','details'])
    index={}
    for stage,field in [('hierarchy','groups'),('mapping','filter_groups'),('voucher','official_vouchers'),('voucher','diagnostic_vouchers')]:
        for position,item in enumerate(stages.get(stage,{}).get(field,[])):
            identity=item['evidence_id']
            if identity in index:raise ValueError('证据ID重复')
            index[identity]={'artifact':'stage_'+stage+'.json','pointer':f'/{field}/{position}'}
    rule_index={}
    if config:
        for section in ['rules','mappings']:
            for position,item in enumerate(config[section]):rule_index[item['id']]={'artifact':'config_snapshot.json','pointer':f'/{section}/{position}'}
        if config.get('auxiliary_profile'):rule_index[config['auxiliary_profile']['id']]={'artifact':'config_snapshot.json','pointer':'/auxiliary_profile'}
    write_json(output/'evidence_index.json',{'schema_version':'0.2','run_id':run_id,'evidence':index,'rules':rule_index})
    voucher=stages.get('voucher',{})
    write_json(output/'voucher_balances.json',{'schema_version':'0.2','run_id':run_id,'official':voucher.get('official_vouchers',[]),
        'diagnostic':voucher.get('diagnostic_vouchers',[]),'diagnostic_only_for_diagnostic_section':True})


def artifact_inventory(output):
    return {p.name:{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size}
        for p in sorted(output.iterdir()) if p.is_file() and p.name!='run_manifest.json'}


def write_summary(output,manifest):
    counts=manifest.get('row_counts',{});status=manifest['status']
    label={'completed':'运行完成','review_required':'运行完成，仍需确认','failed':'运行失败，产物仅用于排错'}[status]
    total=counts.get('total')
    lines=['# SmartMap Excel 候选转换运行报告','',label+'。',
        f"运行ID：{manifest['run_id']}；退出码：{manifest['exit_code']}。",
        f"账期：{manifest.get('period') or '未确定'}；月末：{manifest.get('month_end') or '未确定'}。",
        f"输入数据行：{total if total is not None else '未能取得可靠行数'}。",
        f"正式候选：{counts.get('included',0)}；待确认：{counts.get('needs_review',0)}；排除：{counts.get('excluded',0)}；非业务：{counts.get('non_business',0)}。",
        f"诊断候选：{manifest.get('diagnostic_candidate_count',0)}；异常事实：{manifest.get('exception_issue_count',0)}。",'',
        '正式候选用于审核；系统模板适配及导入尚未验收。',
        '查看 candidates.json/csv、pending_rows.json/csv、excluded_rows.json/csv 与 row_decisions.json/csv。',
        '诊断结果单列在 diagnostic_candidates.json/csv，不代替正式候选。',
        '异常见 exceptions.json/csv；原值见 source_rows.json；证据引用见 evidence_index.json。',
        'run_manifest.json为运行状态和产物哈希的唯一清单；failed运行的文件不作为有效候选输入。']
    if manifest.get('fatal_error'):lines+=['','失败原因：'+manifest['fatal_error']['code']+'：'+manifest['fatal_error']['message']]
    # summary始终属于当前新建运行目录，失败时允许修正当前运行的初始说明。
    with (output/'summary.md').open('w',encoding='utf-8') as stream:stream.write('\n'.join(lines)+'\n')
