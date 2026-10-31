"""Apply explicit local edits to a pixel declaration without retyping other rows."""
from __future__ import annotations

import copy
import json

from src.agent.geometry.plan_partition import _fields, _strings, _text, _PLAN_FIELDS

COLLECTIONS = frozenset({'partitions', 'openings', 'space_seeds'})
SCALARS = (_PLAN_FIELDS | {'dimension_chains'}) - COLLECTIONS - {'floor_id'}


def apply_plan_revision(plan: dict, operations: list) -> tuple[dict, dict]:
    """Never infer geometry or relax compiler checks; failures leave input intact."""
    if not isinstance(plan, dict):
        raise ValueError('saved plan must be an object')
    if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
        raise ValueError('supply 1 to 100 explicit local operations')
    json.dumps(operations, allow_nan=False)
    result = copy.deepcopy(plan)
    touched, changes = set(), []
    for index, raw in enumerate(operations):
        common = {'op', 'reason', 'source_refs'}
        if not isinstance(raw, dict):
            raise ValueError(f'operation {index} must be an object')
        op = raw.get('op')
        extra = {'set': {'field', 'value'}, 'add': {'collection', 'value'},
                 'update': {'collection', 'id', 'changes'},
                 'remove': {'collection', 'id'}}.get(op)
        if extra is None:
            raise ValueError('op must be add, update, remove or set')
        required = frozenset(common | extra)
        _fields(raw, path=f'operation {index}', allowed=required, required=required)
        _text(raw['reason'], path='reason')
        _strings(raw['source_refs'], path='source_refs', require_one=True)
        if op == 'set':
            field = raw['field']
            if field not in SCALARS:
                raise ValueError('set requires an editable top-level field; collections require ID edits')
            key = (field, None)
            before = copy.deepcopy(result.get(field))
            result[field] = copy.deepcopy(raw['value'])
            after = result[field]
        else:
            field = raw['collection']
            if field not in COLLECTIONS:
                raise ValueError('collection must be partitions, openings or space_seeds')
            rows = result.setdefault(field, [])
            if not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get('id'), str) for r in rows):
                raise ValueError(f'{field} needs rows with stable IDs before local revision')
            ids = [r['id'] for r in rows]
            if len(ids) != len(set(ids)):
                raise ValueError(f'{field} has duplicate IDs')
            if op == 'add':
                value = raw['value']
                if not isinstance(value, dict):
                    raise ValueError('add value must be a complete row')
                identity = _text(value.get('id'), path='added row.id')
                if identity in ids:
                    raise ValueError(f'{field}/{identity} already exists')
                before, after = None, copy.deepcopy(value)
                rows.append(after)
            else:
                identity = _text(raw['id'], path='id')
                if identity not in ids:
                    raise ValueError(f'{field}/{identity} does not exist')
                position = ids.index(identity)
                before = copy.deepcopy(rows[position])
                if op == 'remove':
                    rows.pop(position); after = None
                else:
                    delta = raw['changes']
                    if not isinstance(delta, dict) or not delta or 'id' in delta:
                        raise ValueError('update requires nonempty changes without id; remove/add to change identity')
                    rows[position].update(copy.deepcopy(delta))
                    after = copy.deepcopy(rows[position])
            key = (field, identity)
        if key in touched:
            raise ValueError(f'each field/row may be edited once per batch: {key}')
        touched.add(key)
        changes.append(dict(operation_index=index, field=field, id=key[1],
            before=before, after=after, reason=raw['reason'], source_refs=raw['source_refs']))
    preserved = {}
    for field in COLLECTIONS:
        old = {r['id']:r for r in plan.get(field, [])}
        new = {r['id']:r for r in result.get(field, [])}
        unchanged = [key for key in old if (field,key) not in touched]
        assert all(new.get(key) == old[key] for key in unchanged)
        preserved[field] = unchanged
    return result, dict(changes=changes, unchanged_ids=preserved,
        unchanged_fields=[key for key in plan if key not in {field for field,_ in touched}],
        scope='Declaration preservation only; changed topology can change derived rooms/hosts. Compiler validates geometry; original-image fidelity is not certified.')
