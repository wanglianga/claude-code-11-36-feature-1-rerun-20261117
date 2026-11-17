# 样例数据（全部为虚构病例）

由 `python -m scripts.export_samples` 生成，供人工复核与复现。
文件：

- `centers.json`：三个中心的显示名与日期格式配置
- `mappings.json`：各中心本地编码到研究编码的映射
- `protocol-v1.0.json`：方案 v1.0 的入排、终点与随访规则
- `protocol-v1.1.json`：方案 v1.1（原生条件树：阈值放宽/年龄收紧/索引日期迁移/新增 HBA1C）
- `protocol-v1.2.json`：方案 v1.2（扁平形式的两个参数微调）
- `upload-*.json`：各批次上传的原始行（含中心编码差异、单位边界、撤回、迟到病例、
  修订评估用的 wk11 新病例、wk15 补传与更正、wk16 评估后补传）
