"""候选转换与独立评估的接口契约；转换请求已由统一执行器使用。"""
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ConversionRequest:
    source: Path
    config: Path
    output_dir: Path
    period: str | None = None
    allow_partial: bool = False

    def __post_init__(self):
        if type(self.allow_partial) is not bool:raise ValueError('allow_partial必须为明确布尔选择')
        if not all(isinstance(value,Path) for value in [self.source,self.config,self.output_dir]):
            raise ValueError('输入与输出必须使用Path对象')
        if self.period is not None and (not isinstance(self.period,str) or not re.fullmatch(r'[1-9][0-9]{3}(?:0[1-9]|1[0-2])',self.period)):
            raise ValueError('账期必须为YYYYMM；与文件账期冲突时后续引擎须拒绝')
        if self.source.resolve()==self.config.resolve() or self.output_dir.resolve() in [self.source.resolve(),self.config.resolve()]:
            raise ValueError('输入与输出路径不能相同')


@dataclass(frozen=True)
class EvaluationRequest:
    candidate_report: Path
    manual_workbook: Path
    manual_sheet: str
    output_dir: Path
    historical_final: Path | None = None


# 逐行决策为终态；解析不明或业务不明进入needs_review，不可落入excluded。
ROW_DECISIONS = ('included','excluded','needs_review','non_business')
MONEY_STATES = ('blank','number','invalid')
# 金额JSON值使用十进制字符串；status保留空白/零/非法差别。
PROVENANCE_FIELDS = ('input_sha256','sheet','excel_row')
