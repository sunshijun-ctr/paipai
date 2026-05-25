# 多用户数据隔离设计方案

> 技术栈：Python · PostgreSQL · 邮箱登录 · 本地文件存储（后期迁移云存储）

---

## 目录

1. [整体架构](#整体架构)
2. [数据库设计](#数据库设计)
3. [用户认证模块](#用户认证模块)
4. [对话记录隔离](#对话记录隔离)
5. [文件存储隔离与容量控制](#文件存储隔离与容量控制)
6. [API 接口设计](#api-接口设计)
7. [云存储迁移指南](#云存储迁移指南)
8. [安全规范](#安全规范)

---

## 整体架构

```
用户登录（邮箱 + 密码）
        ↓
  生成 JWT Token
        ↓
  每次请求携带 Token（Authorization: Bearer <token>）
        ↓
  服务端验证 Token → 提取 user_id
        ↓
  对话记录 / 文件 / 存储用量 全部以 user_id 隔离
```

**核心原则：** 所有数据操作必须在服务端以 `user_id` 为边界过滤，前端传入的用户信息不可信。

---

## 数据库设计

使用 PostgreSQL，所有涉及用户数据的表都以 `user_id` 作为外键关联。

```sql
-- 启用 UUID 扩展
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================
-- 用户表
-- ============================================================
CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,                         -- bcrypt 哈希
    storage_limit   BIGINT NOT NULL DEFAULT 524288000,     -- 500MB（字节）
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- 存储用量表（独立维护，避免每次扫目录统计）
-- ============================================================
CREATE TABLE user_storage (
    user_id         UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    used_bytes      BIGINT NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- 对话表
-- ============================================================
CREATE TABLE conversations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_conversations_user_id ON conversations(user_id);

-- ============================================================
-- 消息表
-- ============================================================
CREATE TABLE messages (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id     UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role                TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content             TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_messages_conversation_id ON messages(conversation_id);
CREATE INDEX idx_messages_user_id ON messages(user_id);

-- ============================================================
-- 文件表
-- ============================================================
CREATE TABLE files (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    original_name   TEXT NOT NULL,                 -- 用户上传时的原始文件名
    storage_key     TEXT NOT NULL,                 -- 本地路径 or 云存储 object key
    size_bytes      BIGINT NOT NULL,
    mime_type       TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_files_user_id ON files(user_id);
```

> **迁移提示：** 本地存储时 `storage_key` 存相对路径（如 `users/{user_id}/{uuid}.pdf`）；迁移云存储后同字段改为 object key，表结构无需变动。

---

## 用户认证模块

### 依赖安装

```bash
pip install fastapi python-jose[cryptography] passlib[bcrypt] asyncpg python-multipart aiofiles
```

### `auth.py` — Token 签发与验证

```python
import uuid
from datetime import datetime, timedelta
from jose import JWTError, jwt
from passlib.context import CryptContext

SECRET_KEY = "your-secret-key-please-change-in-production"
ALGORITHM  = "HS256"
TOKEN_EXPIRE_DAYS = 7

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_token(user_id: str, email: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "exp": datetime.utcnow() + timedelta(days=TOKEN_EXPIRE_DAYS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
```

### `deps.py` — FastAPI 依赖注入

```python
from fastapi import Depends, HTTPException, Header
from auth import decode_token


async def get_current_user(authorization: str = Header(...)) -> dict:
    """
    所有需要认证的接口都依赖此函数。
    从 Authorization: Bearer <token> 中解析出 user_id。
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token 格式错误")

    token = authorization.split(" ", 1)[1]
    payload = decode_token(token)

    if payload is None:
        raise HTTPException(status_code=401, detail="Token 已过期或无效")

    return {"user_id": payload["sub"], "email": payload["email"]}
```

---

## 对话记录隔离

所有查询**强制携带** `user_id` 条件，杜绝越权访问。

```python
# conversations.py
from fastapi import APIRouter, Depends
from deps import get_current_user
import asyncpg

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("/")
async def list_conversations(
    db: asyncpg.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    # ✅ WHERE user_id = $1 强制隔离
    rows = await db.fetch(
        """
        SELECT id, title, created_at
        FROM conversations
        WHERE user_id = $1
        ORDER BY updated_at DESC
        """,
        user["user_id"],
    )
    return [dict(r) for r in rows]


@router.get("/{conversation_id}/messages")
async def get_messages(
    conversation_id: str,
    db: asyncpg.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    # ✅ 同时校验 conversation_id 和 user_id，防止跨用户访问
    rows = await db.fetch(
        """
        SELECT m.id, m.role, m.content, m.created_at
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        WHERE m.conversation_id = $1 AND c.user_id = $2
        ORDER BY m.created_at ASC
        """,
        conversation_id,
        user["user_id"],
    )
    return [dict(r) for r in rows]


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    db: asyncpg.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    # ✅ 只允许删除自己的对话
    result = await db.execute(
        "DELETE FROM conversations WHERE id = $1 AND user_id = $2",
        conversation_id,
        user["user_id"],
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="对话不存在")
    return {"message": "已删除"}
```

---

## 文件存储隔离与容量控制

### 目录结构

```
./user_storage/
  {user_id_1}/
    a1b2c3d4.pdf
    e5f6g7h8.png
  {user_id_2}/
    ...
```

### `storage.py` — 核心存储逻辑

```python
import uuid
import aiofiles
from pathlib import Path
from fastapi import HTTPException, UploadFile
import asyncpg

STORAGE_ROOT  = Path("./user_storage")
STORAGE_LIMIT = 500 * 1024 * 1024  # 500MB（字节）


def get_user_dir(user_id: str) -> Path:
    """获取用户专属目录，自动创建"""
    path = STORAGE_ROOT / user_id
    path.mkdir(parents=True, exist_ok=True)
    return path


async def get_used_bytes(db: asyncpg.Connection, user_id: str) -> int:
    """查询用户已用存储（字节）"""
    row = await db.fetchrow(
        "SELECT used_bytes FROM user_storage WHERE user_id = $1",
        user_id,
    )
    return row["used_bytes"] if row else 0


async def check_quota(db: asyncpg.Connection, user_id: str, new_size: int):
    """上传前检查是否超出配额，超出则抛出 400"""
    used = await get_used_bytes(db, user_id)
    if used + new_size > STORAGE_LIMIT:
        used_mb  = used / 1024 / 1024
        new_mb   = new_size / 1024 / 1024
        raise HTTPException(
            status_code=400,
            detail=(
                f"存储空间不足：已用 {used_mb:.1f}MB，"
                f"本次上传 {new_mb:.1f}MB，超出 500MB 限制"
            ),
        )


async def save_file(
    db: asyncpg.Connection,
    user_id: str,
    file: UploadFile,
) -> dict:
    """
    上传文件主流程：
    1. 检查配额
    2. 以 UUID 命名写入磁盘（防路径穿越）
    3. 事务写入 DB + 更新用量
    """
    content   = await file.read()
    file_size = len(content)

    # 1. 配额检查
    await check_quota(db, user_id, file_size)

    # 2. 安全存储：文件名用 UUID，保留原始扩展名
    file_id  = str(uuid.uuid4())
    suffix   = Path(file.filename).suffix.lower()
    safe_name = f"{file_id}{suffix}"
    file_path = get_user_dir(user_id) / safe_name

    async with aiofiles.open(file_path, "wb") as f:
        await f.write(content)

    # 3. 事务：写文件记录 + 原子更新用量
    async with db.transaction():
        await db.execute(
            """
            INSERT INTO files (id, user_id, original_name, storage_key, size_bytes, mime_type)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            file_id, user_id, file.filename,
            str(file_path), file_size, file.content_type,
        )
        await db.execute(
            """
            INSERT INTO user_storage (user_id, used_bytes)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE
              SET used_bytes = user_storage.used_bytes + $2,
                  updated_at = NOW()
            """,
            user_id, file_size,
        )

    return {
        "file_id":   file_id,
        "filename":  file.filename,
        "size_bytes": file_size,
    }


async def delete_file(
    db: asyncpg.Connection,
    user_id: str,
    file_id: str,
) -> dict:
    """
    删除文件：校验归属 → 删物理文件 → 事务更新 DB
    """
    row = await db.fetchrow(
        "SELECT * FROM files WHERE id = $1 AND user_id = $2",
        file_id, user_id,  # ✅ 强制校验归属
    )
    if not row:
        raise HTTPException(status_code=404, detail="文件不存在或无权访问")

    # 删除物理文件
    Path(row["storage_key"]).unlink(missing_ok=True)

    # 事务：删记录 + 释放用量
    async with db.transaction():
        await db.execute("DELETE FROM files WHERE id = $1", file_id)
        await db.execute(
            """
            UPDATE user_storage
            SET used_bytes = GREATEST(used_bytes - $1, 0),
                updated_at = NOW()
            WHERE user_id = $2
            """,
            row["size_bytes"], user_id,
        )

    return {"message": "文件已删除", "freed_bytes": row["size_bytes"]}
```

---

## API 接口设计

```python
# main.py
from fastapi import FastAPI, Depends, UploadFile
from deps import get_current_user
from storage import save_file, delete_file, get_used_bytes

app = FastAPI()

STORAGE_LIMIT = 500 * 1024 * 1024


# ── 文件上传 ─────────────────────────────────────────────────
@app.post("/files/upload")
async def upload_file(
    file: UploadFile,
    db=Depends(get_db),
    user=Depends(get_current_user),
):
    return await save_file(db, user["user_id"], file)


# ── 文件列表（只返回自己的）────────────────────────────────────
@app.get("/files")
async def list_files(db=Depends(get_db), user=Depends(get_current_user)):
    rows = await db.fetch(
        """
        SELECT id, original_name, size_bytes, mime_type, created_at
        FROM files
        WHERE user_id = $1
        ORDER BY created_at DESC
        """,
        user["user_id"],
    )
    return [dict(r) for r in rows]


# ── 删除文件 ──────────────────────────────────────────────────
@app.delete("/files/{file_id}")
async def remove_file(
    file_id: str,
    db=Depends(get_db),
    user=Depends(get_current_user),
):
    return await delete_file(db, user["user_id"], file_id)


# ── 存储用量查询 ──────────────────────────────────────────────
@app.get("/storage/usage")
async def get_storage_usage(db=Depends(get_db), user=Depends(get_current_user)):
    used  = await get_used_bytes(db, user["user_id"])
    limit = STORAGE_LIMIT
    return {
        "used_bytes":  used,
        "used_mb":     round(used / 1024 / 1024, 1),
        "limit_mb":    500,
        "percent":     round(used / limit * 100, 1),
        "free_mb":     round((limit - used) / 1024 / 1024, 1),
    }
```

**响应示例：**

```json
{
  "used_bytes": 157286400,
  "used_mb": 150.0,
  "limit_mb": 500,
  "percent": 30.0,
  "free_mb": 350.0
}
```

---

## 云存储迁移指南

迁移时只需修改 `storage.py` 中的写入/删除逻辑，数据库结构和所有 API 接口**完全不变**。

### 迁移步骤

**Step 1：安装 SDK**

```bash
# AWS S3
pip install boto3

# 阿里云 OSS
pip install oss2
```

**Step 2：替换 `save_file` 中的写磁盘逻辑**

```python
# 本地（当前）
async with aiofiles.open(file_path, "wb") as f:
    await f.write(content)
storage_key = str(file_path)

# ↓ 迁移后替换为（以 S3 为例）
import boto3
s3 = boto3.client("s3", region_name="ap-northeast-1")
BUCKET = "your-bucket-name"

object_key = f"users/{user_id}/{safe_name}"
s3.put_object(Bucket=BUCKET, Key=object_key, Body=content)
storage_key = object_key   # ← DB 里存 object_key，结构不变
```

**Step 3：替换 `delete_file` 中的删除逻辑**

```python
# 本地（当前）
Path(row["storage_key"]).unlink(missing_ok=True)

# ↓ 迁移后替换为
s3.delete_object(Bucket=BUCKET, Key=row["storage_key"])
```

**Step 4：文件下载改为生成预签名 URL**

```python
@app.get("/files/{file_id}/url")
async def get_download_url(
    file_id: str,
    db=Depends(get_db),
    user=Depends(get_current_user),
):
    row = await db.fetchrow(
        "SELECT storage_key FROM files WHERE id = $1 AND user_id = $2",
        file_id, user["user_id"],
    )
    if not row:
        raise HTTPException(404, "文件不存在")

    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": row["storage_key"]},
        ExpiresIn=3600,  # 1 小时有效
    )
    return {"url": url}
```

---

## 安全规范

| 风险 | 措施 |
|------|------|
| 越权访问他人对话 | 所有查询强制加 `WHERE user_id = $1`，从 Token 中取值 |
| 越权访问他人文件 | 删除/下载前校验 `files.user_id = current_user_id` |
| 绕过容量限制 | 配额检查 + 用量更新在同一个数据库事务内完成 |
| 路径穿越攻击 | 文件名统一用 UUID 重命名，不使用用户传入的文件名 |
| Token 伪造 | 服务端用 `SECRET_KEY` 验证签名，Token 中的 `user_id` 不可篡改 |
| 并发超量上传 | PostgreSQL 使用 `GREATEST(used_bytes - size, 0)` 防止负值，上传加乐观锁或行锁 |
| 密码泄露 | 密码只存 bcrypt 哈希，永不明文落库 |

### 核心原则

```
永远不要相信前端传入的 user_id。
永远从 JWT Token 中解析 user_id，再用它做数据过滤。
```

---

*文档版本 v1.0 · 适用技术栈：Python / FastAPI · PostgreSQL · JWT 邮箱认证*