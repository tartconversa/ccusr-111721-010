# 跨机构数据协作台账

本服务记录数据来源、字段含义、合作许可、数据产品及交付凭据。合作方可在本地演练不同机构和法域之间的资料流转；示例记录均为虚构，不连接真实口岸或支付系统。

需要 Python 3.10 或更新版本，不依赖第三方包。

运行项目测试：`python -m unittest discover -s tests -v`。
检查源码：`python -m compileall -q exchange`。
演练入口：`python -m exchange --database demo.db`。

领域逻辑分布在来源目录、合作许可、产品编排和交付核验中。`exchange/service.py` 组合这些操作，`exchange/db.py` 保存本地 SQLite 表结构；调用接口和现有边界可从源码及测试读取。

