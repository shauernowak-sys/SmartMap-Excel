"""仅用于测试的微型OOXML工厂；写临时合成数据，不接触企业样本。"""
from datetime import date, datetime
from xml.sax.saxutils import escape, quoteattr
from zipfile import ZipFile, ZIP_DEFLATED

S='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def column(number):
    result=''
    while number:number,rem=divmod(number-1,26);result=chr(65+rem)+result
    return result


def write_xlsx(path,sheets,dimension=None):
    style=f'''<styleSheet xmlns="{S}"><numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd"/></numFmts><fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''
    with ZipFile(path,'w',ZIP_DEFLATED) as archive:
        overrides=''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,len(sheets)+1))
        archive.writestr('[Content_Types].xml',f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>{overrides}</Types>')
        archive.writestr('_rels/.rels',f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{R}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml',f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets>'+''.join(f'<sheet name={quoteattr(name)} sheetId="{i}" r:id="rId{i}"/>' for i,(name,_) in enumerate(sheets,1))+'</sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(f'<Relationship Id="rId{i}" Type="{R}/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(sheets)+1))+f'<Relationship Id="styles" Type="{R}/styles" Target="styles.xml"/></Relationships>')
        archive.writestr('xl/styles.xml',style)
        for index,(_,rows) in enumerate(sheets,1):
            body=[]
            for number,row in enumerate(rows,1):
                cells=[]
                for col,value in enumerate(row,1):
                    address=column(col)+str(number)
                    if value is None:continue
                    if isinstance(value,dict):
                        kind=value['type'];v=escape(str(value['value']))
                        content=f'<f>{v}</f>' if kind=='f' else f'<v>{v}</v>'
                        cells.append(f'<c r="{address}"'+('' if kind=='f' else f' t="{kind}"')+f'>{content}</c>')
                    elif isinstance(value,(date,datetime)):
                        d=value.date() if isinstance(value,datetime) else value
                        cells.append(f'<c r="{address}" s="1"><v>{(d-date(1899,12,30)).days}</v></c>')
                    elif isinstance(value,bool):cells.append(f'<c r="{address}" t="b"><v>{int(value)}</v></c>')
                    elif isinstance(value,(int,float)):cells.append(f'<c r="{address}"><v>{value}</v></c>')
                    else:cells.append(f'<c r="{address}" t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>')
                body.append(f'<row r="{number}">'+''.join(cells)+'</row>')
            size=dimension or f'A1:{column(max((len(r) for r in rows),default=1))}{len(rows)}'
            archive.writestr(f'xl/worksheets/sheet{index}.xml',f'<worksheet xmlns="{S}"><dimension ref="{size}"/><sheetData>'+''.join(body)+'</sheetData></worksheet>')
