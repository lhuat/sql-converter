"""Optional offline benchmark; uses synthetic SQL, never customer data.

Run from project root:
    python -m benchmarks.benchmark_conversion --size-mib 20
This measures local throughput; results vary by disk, CPU and input format.
"""
from __future__ import annotations
import argparse
import hashlib
import tempfile
import time
from pathlib import Path
from sql_converter.engine import convert_file


def make_dump(path: Path, target_bytes: int) -> None:
    with path.open('w', encoding='utf-8', newline='\n') as dst:
        dst.write("CREATE TABLE `bench` (`id` BIGINT, `name` VARCHAR(80), `payload` TEXT);\n")
        row_id = 0
        while dst.tell() < target_bytes:
            # Representative multi-row INSERT; each statement fits the safe
            # statement cap while containing Unicode, JSON, and SQL escapes.
            dst.write('INSERT INTO `bench` VALUES\n')
            rows = []
            for _ in range(1000):
                row_id += 1
                rows.append(f"({row_id}, '中文{row_id}', '{{\\\"path\\\":\\\"C:\\\\\\\\data\\\",\\\"key\\\":{row_id}}}')")
            dst.write(',\n'.join(rows))
            dst.write(';\n')


def run(size_mib: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        input_path = path / 'bench.sql'
        make_dump(input_path, size_mib * 1024 * 1024)
        size = input_path.stat().st_size
        started = time.perf_counter()
        sql_path, report_path, report = convert_file(input_path, path, 'mysql_to_opengauss', encoding='utf-8')
        elapsed = time.perf_counter() - started
        digest = hashlib.sha256(sql_path.read_bytes()).hexdigest()
        print(f'input={size/1024**2:.1f} MiB elapsed={elapsed:.2f}s '
              f'throughput={size/1024**2/max(elapsed,0.001):.1f} MiB/s '
              f'statements={report.total} blocked={report.blocked} sha256={digest}')
        print(f'output={sql_path.stat().st_size/1024**2:.1f} MiB report={report_path.name}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--size-mib', type=int, default=20)
    args = parser.parse_args()
    if not (1 <= args.size_mib <= 400):
        parser.error('--size-mib must be between 1 and 400')
    run(args.size_mib)
