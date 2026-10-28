"""各处理阶段共用凭证归属键，不以凭证数字单独串组。"""


def voucher_key(row):
    voucher=row.get('voucher')
    if not voucher:return None
    return (row['provenance']['input_sha256'],row['provenance']['sheet'],row['period'],voucher['category'],voucher['number'])
