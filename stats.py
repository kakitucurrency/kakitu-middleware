from dataclasses import dataclass, field
from typing import List


@dataclass
class Stats:
    total_work_completed: int = 0
    total_kshs_paid: float = 0.0
    _earners: dict = field(default_factory=dict)  # address → {earned, work_completed}
    connected_workers: int = 0  # set externally by WorkerPool

    def record_completion(self, worker, amount: float):
        self.total_work_completed += 1
        self.total_kshs_paid += amount
        worker.work_completed += 1
        worker.kshs_earned += amount

        addr = worker.kshs_address
        if addr not in self._earners:
            self._earners[addr] = {'earned': 0.0, 'work_completed': 0}
        self._earners[addr]['earned'] += amount
        self._earners[addr]['work_completed'] += 1

    def to_dict(self) -> dict:
        top = sorted(
            self._earners.items(),
            key=lambda x: x[1]['earned'],
            reverse=True
        )[:10]
        return {
            'connected_workers': self.connected_workers,
            'total_work_completed': self.total_work_completed,
            'total_kshs_paid': f'{self.total_kshs_paid:.6f}',
            'top_earners': [
                {
                    'address': addr,
                    'earned': f'{data["earned"]:.6f}',
                    'work_completed': data['work_completed'],
                }
                for addr, data in top
            ],
        }
