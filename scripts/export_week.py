import os
import sys
import json
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timedelta


def week_range():
    now = datetime.now()
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")


def http_get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def http_post(url):
    req = urllib.request.Request(url, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    p = argparse.ArgumentParser(description="导出本周对话，供 AI 分析；可选归档删除")
    p.add_argument("--host", default="http://127.0.0.1:5000", help="网关地址")
    p.add_argument("--start", help="起始时间 YYYY-MM-DD HH:MM:SS，默认本周一")
    p.add_argument("--end", help="结束时间，默认现在")
    p.add_argument("--out", help="导出文件路径，默认桌面")
    p.add_argument("--archive", action="store_true", help="导出后归档并从数据库删除")
    args = p.parse_args()

    start, end = week_range()
    if args.start:
        start = args.start
    if args.end:
        end = args.end

    host = args.host.rstrip("/")
    from urllib.parse import quote
    qs = f"start={quote(start)}&end={quote(end)}"

    print(f"导出范围: {start}  ->  {end}")
    try:
        data = http_get(f"{host}/api/conversations/export?{qs}")
    except urllib.error.URLError as e:
        print(f"连接网关失败: {e}\n请确认 AI Gateway 正在运行 ({host})")
        sys.exit(1)

    count = data.get("count", 0)
    if args.out:
        out_path = args.out
    else:
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        if not os.path.isdir(desktop):
            desktop = os.getcwd()
        out_path = os.path.join(desktop, f"conversations_week_{start[:10]}.json")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"已导出 {count} 条对话 -> {out_path}")

    if args.archive:
        if count == 0:
            print("无数据，跳过归档")
            return
        ans = input(f"确认归档并从数据库删除这 {count} 条对话? (yes/N): ").strip().lower()
        if ans != "yes":
            print("已取消归档，数据保留")
            return
        res = http_post(f"{host}/api/conversations/archive?{qs}")
        print(f"已归档 {res.get('archived', 0)} 条 -> {res.get('file')}")


if __name__ == "__main__":
    main()
