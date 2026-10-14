# 项目约束

## 数据库测试脚本规则

所有涉及数据库操作的测试脚本，必须遵守以下规则：

1. 禁止手写表字段名。必须从 `src.backend_v2.storage.schema` import 真实表对象。
2. 禁止手写枚举值。有 CHECK 约束的字段必须从 schema 定义 import 合法常量。
3. 写任何数据库脚本前，先执行 `grep -n "Table(" src/backend_v2/storage/schema.py` 确认字段定义。
4. 写任何读取 JSON 产物的脚本前，先 `cat` 那个 JSON 文件确认字段名。
5. 同一个脚本失败超过两次，立即停止，贴出完整报错人工排查。

### 正确示例

```python
from src.backend_v2.storage.schema import books, users

conn.execute(
    books.insert().values(
        id="test-book-001",
        owner_user_id=users.c.id,  # 从真实表对象引用
        kind="standard",           # 如果有枚举常量，优先用常量
        title="测试书籍",
    )
)