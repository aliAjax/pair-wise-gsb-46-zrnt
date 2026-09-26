"""应用入口：参数解析、依赖组装与HTTP服务生命周期。"""
import argparse
from pathlib import Path

from src.air_rules import AirRules
from src.air_service import AirService
from src.audit import AuditRecorder
from src.http_api import create_server
from src.repository import Repository
from src.rules import DomainRules
from src.service import Service


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "ambulance-dispatch.db"
DEFAULT_PORT = 8322


def build_service(db_path: str) -> Service:
    repository = Repository(db_path)
    audit = AuditRecorder(repository)
    return Service(repository, DomainRules(), audit)


def build_air_service(db_path: str, repository: Repository = None) -> AirService:
    return AirService(repository or Repository(db_path), AirRules())


def parse_args():
    parser = argparse.ArgumentParser(description="空地转运台：急救车与直升机调度分流")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite数据库路径")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP监听端口")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    Path(args.db).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    repository = Repository(args.db)
    service = Service(repository, DomainRules(), AuditRecorder(repository))
    air_service = AirService(repository, AirRules())
    server = create_server(args.host, args.port, service, BASE_DIR / "static", air_service)
    print("空地转运台 listening on http://%s:%s" % (args.host, args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
