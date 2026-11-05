"""Cross-employee SFace conflicts; same-ID aliases are one identity."""
from dataclasses import dataclass

import numpy as np

from .catalog import legacy_employee_id


@dataclass(frozen=True)
class DuplicateFaceMatch:
    label: str
    employee_id: str
    employee_name: str
    matched_label: str
    matched_employee_id: str
    matched_employee_name: str
    distance: float


class FaceDuplicateIndex:
    """Normalize once; comparisons use cosine distance, as live recognition does."""
    def __init__(self, samples, records):
        vectors, owners = [], []
        for label, values in samples.items():
            employee = records.get(label)
            owner = (label, employee.employee_id if employee else legacy_employee_id(label),
                     employee.name if employee else label)
            for value in values:
                vector = np.asarray(value, dtype=np.float64)
                if vector.shape != (128,) or not np.isfinite(vector).all():
                    continue
                norm = float(np.linalg.norm(vector))
                if not np.isfinite(norm) or norm < 1e-8:
                    continue
                vectors.append(vector / norm)
                owners.append(owner)
        self.vectors = np.asarray(vectors).reshape(-1,128)
        self.owners = owners
        self.employee_ids = np.asarray([owner[1] for owner in owners],dtype=object)
        self.labels = np.asarray([owner[0] for owner in owners],dtype=object)

    @staticmethod
    def _threshold(threshold):
        if not 0 < float(threshold) < 2:
            raise ValueError('Duplicate face distance must be between 0 and 2')

    def match(self, vector, employee_id, threshold=.5, label='', employee_name=''):
        self._threshold(threshold)
        vector = np.asarray(vector,dtype=np.float64)
        if vector.shape != (128,) or not np.isfinite(vector).all():
            raise ValueError('Invalid SFace embedding')
        norm = float(np.linalg.norm(vector))
        if not np.isfinite(norm) or norm < 1e-8:
            raise ValueError('Invalid SFace embedding')
        if not self.owners:
            return None
        distances = np.clip(1 - self.vectors @ (vector / norm),0,2)
        # An unmapped label may be deliberately assigned its first permanent ID.
        indices = np.flatnonzero((self.employee_ids != employee_id) & (self.labels != label)
                                 & (distances <= threshold))
        eligible = [(float(distances[i]),self.owners[i]) for i in indices]
        if not eligible:
            return None
        distance, owner = min(eligible,key=lambda item:(item[0],item[1]))
        return DuplicateFaceMatch(label,employee_id,employee_name,*owner,distance)

    def conflicts(self, threshold=.5):
        """Closest sample per label pair; bounded matrices avoid an N×N allocation."""
        self._threshold(threshold)
        result = {}
        for start in range(0,len(self.owners),256):
            left = self.vectors[start:start+256]
            for offset in range(start,len(self.owners),256):
                distances = np.clip(1-left @ self.vectors[offset:offset+256].T,0,2)
                same_owner = (self.employee_ids[start:start+256,None]
                              == self.employee_ids[None,offset:offset+256])
                distances[same_owner] = np.inf
                for i,j in zip(*np.where(distances <= threshold)):
                    if start+i >= offset+j:
                        continue
                    first,second = self.owners[start+i],self.owners[offset+j]
                    if first[1] == second[1]:
                        continue
                    first,second = sorted((first,second))
                    key = (first[0],second[0])
                    distance = float(distances[i,j])
                    if key not in result or distance < result[key].distance:
                        result[key] = DuplicateFaceMatch(*first,*second,distance)
        return sorted(result.values(),key=lambda match:(match.label.casefold(),match.matched_label.casefold()))


def find_face_duplicate(vector, samples, records, employee_id, threshold=.5,
                        label='', employee_name=''):
    return FaceDuplicateIndex(samples,records).match(vector,employee_id,threshold,label,employee_name)
