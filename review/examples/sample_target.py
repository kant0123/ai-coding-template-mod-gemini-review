# サンプル被監査コード (複合的クリティカル欠陥を含む)
#
# **意図的に壊してあるフィクスチャ。実行されないし、どこからも import されない。**
# 下の SECRET_KEY はダミー文字列であって実在の秘密情報ではない。
# selfcheck ワークフローがこのファイルにプレスキャナを掛け、Critical が検出されることを
# 確認している(検出ロジックが黙って壊れて「実行できるが何も見ていない」状態で
# 配られるのを防ぐため)。欠陥を減らすとその煙試験が意味を失うので、直さないこと。
import os
import hmac
from decimal import Decimal

# 欠陥1: ハードコードされた秘密情報
SECRET_KEY = "my-super-secret-key-12345"

async def authenticate(user_token: str) -> bool:
    # 欠陥2: Timing Attack (通常の == 比較)
    if user_token == SECRET_KEY:
        return True
    return False

async def calculate_interest_and_withdraw(account_id: int, rate: float, days: int, db):
    # 欠陥3: 金融計算における float の使用
    balance = float(await db.fetch_val("SELECT balance FROM accounts WHERE id = :id", {"id": account_id}))
    # 欠陥4: TOCTOU (ロックなしの出金)
    interest = balance * (rate / 365.0) * days
    new_balance = balance + interest
    await db.execute("UPDATE accounts SET balance = :b WHERE id = :id", {"b": new_balance, "id": account_id})
    return new_balance

async def fetch_user_orders(user_ids: list, db):
    # 欠陥5: N+1 クエリ (ループ内SQL)
    results = []
    for uid in user_ids:
        orders = await db.fetch_all("SELECT * FROM orders WHERE user_id = :uid", {"uid": uid})
        results.append(orders)
    return results
