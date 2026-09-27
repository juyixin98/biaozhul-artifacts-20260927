"""服务的错误类别（分层定义，测试按类别断言，而不是笼统的失败）。"""


class Mp4TimelineError(Exception):
    """本项目所有错误的基类。"""


# ---- 媒体解析层 ----


class ParseError(Mp4TimelineError):
    """解析失败的基类。"""


class BoxLengthError(ParseError):
    """盒声明长度小于盒头，或超出父容器 / 文件边界。"""


class UnsupportedLayoutError(ParseError):
    """布局不在本服务支持范围内：分片 MP4（moof/mvex）、加密样本等。"""


class MissingBoxError(ParseError):
    """必需的盒不存在（如 moov、stsz、mdhd）。"""


class SampleTableError(ParseError):
    """样本表内部不一致（stsc/stsz/stco/stts 样本数对不上等）。"""


class EditListError(ParseError):
    """edit list 非法（未知版本、不支持的非 1.0 媒体速率等）。"""


# ---- 作业层 ----


class JobError(Mp4TimelineError):
    """作业层错误基类。"""


class JobNotFoundError(JobError):
    """作业 id 不存在。"""
