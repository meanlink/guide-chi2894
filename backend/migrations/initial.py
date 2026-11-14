from sqlalchemy import inspect, text
from src.config.database import Base, engine
from src import models
def migrate():
    Base.metadata.create_all(engine)
    # 既有库的 deliveries 表补充期次/状态列；新增列可空，历史归档回填为 archived
    json_type = 'JSON' if engine.dialect.name == 'postgresql' else 'TEXT'
    columns = {
        'status': 'ALTER TABLE deliveries ADD COLUMN status VARCHAR(20)',
        'error': "ALTER TABLE deliveries ADD COLUMN error VARCHAR(1000) NOT NULL DEFAULT ''",
        'checksum_json': "ALTER TABLE deliveries ADD COLUMN checksum_json VARCHAR(64) NOT NULL DEFAULT ''",
        'scheduled_for': 'ALTER TABLE deliveries ADD COLUMN scheduled_for DOUBLE PRECISION',
        'snapshot': f'ALTER TABLE deliveries ADD COLUMN snapshot {json_type}',
    }
    with engine.begin() as conn:
        existing = {c['name'] for c in inspect(conn).get_columns('deliveries')}
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(text(ddl))
        conn.execute(text("UPDATE deliveries SET status='archived' WHERE status IS NULL"))
        conn.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_deliveries_report_issue ON deliveries (report_id, scheduled_for)'))
if __name__=='__main__':migrate()
