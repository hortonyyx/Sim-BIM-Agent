"""Read-only diagnostics for independent parametric declaration fields.

This is deliberately not another plan schema. Geometry, references, expansion,
and cross-opening constraints remain with the existing compiler/save gate.
Field types come from the actual source schema; the compact row contract and
role-evidence validator below are also used by the parametric compiler itself.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import TypeAdapter, ValidationError

from src.agent.correction.schema import RoomRoleEvidence, WallOpening
from src.agent.geometry.source_schema import SourceWallOpening
from src.agent.roles import require_role


OPENING_ROW_REQUIRED_FIELDS = ('id', 'space', 'p1', 'p2', 'spans', 'z', 'kind', 'source_refs')
OPENING_ROW_OPTIONAL_FIELDS = ('other_space', 'state', 'assumptions')


def core_connection_field_issues(kind, state):
    """The compact core declaration is stricter than generic WallOpening.state."""
    issues = []
    kinds, states = ('door', 'open'), ('open', 'closed', 'unknown')
    if kind not in kinds:
        issues.append(('kind', kinds))
    if state not in states:
        issues.append(('state', states))
    elif kind == 'open' and state != 'open':
        issues.append(('state', ('open',)))
    return issues


def validated_role_evidence(space):
    """The compiler's existing role-evidence contract, without filling values."""
    if 'role_evidence' not in space:
        return {}
    evidence = RoomRoleEvidence.model_validate(space['role_evidence']).model_dump(mode='json')
    if require_role(space['role']) != evidence['role']:
        raise ValueError(f"space {space['id']}: role does not match role_evidence.role")
    return {'role_evidence': evidence}


def _pointer(parts):
    return ''.join('/' + str(part).replace('~', '~0').replace('/', '~1') for part in parts)


@lru_cache(maxsize=None)
def _field_adapter(model, name):
    return TypeAdapter(model.model_fields[name].rebuild_annotation())


def _field_hints(model, name):
    schema = _field_adapter(model, name).json_schema()
    enums = schema.get('enum')
    if enums is None and 'anyOf' in schema:
        variants = schema['anyOf']
        enums = next((variant['enum'] for variant in variants if 'enum' in variant), None)
        if enums is not None and any(variant.get('type') == 'null' for variant in variants):
            enums = [*enums, None]
    return {'allowed_values': enums} if enums is not None else {}


def _validation_diagnostics(error, path, model):
    result = []
    for item in error.errors(include_input=False, include_url=False):
        location = item['loc']
        diagnostic = dict(code='missing_field' if item['type'] == 'missing' else 'invalid_field',
                          pointer=_pointer((*path, *location)), message=item['msg'])
        if item['type'] == 'missing':
            diagnostic['required_fields'] = [name for name, field in model.model_fields.items()
                                             if field.is_required()]
        if location and location[0] in model.model_fields:
            diagnostic.update(_field_hints(model, location[0]))
        result.append(diagnostic)
    return result


def _rows(owner, field):
    """Malformed containers belong to the normal parser, not invented coercion."""
    value = owner.get(field, [])
    return enumerate(value) if isinstance(value, list) else ()


def _source_ref_diagnostics(row, path, *, model=None, validate_value=True):
    """Check the declaration's own refs, before instance refs are appended.

    Compact declarations use the compiler's strict list contract. Direct
    connections use the selected source field, including its coercion rules.
    Neither contract inherits missing refs from a floor or patch operation.
    """
    if not isinstance(row, dict):
        return []
    path = (*path, 'source_refs')
    if 'source_refs' not in row:
        return [dict(code='missing_field', pointer=_pointer(path),
                     message='Field required', required_fields=['source_refs'])]
    if not validate_value:
        return []
    if model is None:
        from src.agent.geometry.parametric_proposal import strings
        try:
            strings(row['source_refs'], True)
        except ValueError as error:
            return [dict(code='invalid_field', pointer=_pointer(path), message=str(error))]
    else:
        try:
            refs = _field_adapter(model, 'source_refs').validate_python(row['source_refs'])
        except ValidationError as error:
            return [dict(code='invalid_field', pointer=_pointer((*path, *item['loc'])),
                         message=item['msg'])
                    for item in error.errors(include_input=False, include_url=False)]
        # Both formal opening models apply this after their field validation.
        if not all(ref.strip() for ref in refs):
            return [dict(code='invalid_field', pointer=_pointer(path),
                         message='opening source references must not be blank')]
    return []


def parametric_field_diagnostics(plan) -> list[dict]:
    """Collect independent opening, source-ref and role-evidence field errors.

    An empty list is not a validity or fidelity verdict. This function can run
    before geometry regularization, including when primitive expansion or ring
    adoption will fail. It does not inspect or replace geometric declarations.
    """
    if not isinstance(plan, dict) or plan.get('schema_version', '2') not in ('2', 'source_polygon_v1'):
        return []
    diagnostics = []
    opening_model = SourceWallOpening if plan.get('schema_version') == 'source_polygon_v1' else WallOpening

    def opening_fields(row, path, *, required_kind=False, model=opening_model):
        if not isinstance(row, dict):
            return
        for name in ('kind', 'state'):
            if name not in row:
                if name == 'kind' and required_kind:
                    diagnostics.append(dict(code='missing_field', pointer=_pointer((*path, name)),
                        message='Field required', required_fields=[name], **_field_hints(model, name)))
                continue
            try:
                _field_adapter(model, name).validate_python(row[name])
            except ValidationError as error:
                for item in error.errors(include_input=False, include_url=False):
                    diagnostics.append(dict(code='invalid_field', pointer=_pointer((*path, name, *item['loc'])),
                        message=item['msg'], **_field_hints(model, name)))

    templates = plan.get('templates', {})
    # Reuse exact repeat/replacement selection: an entirely excluded repeat
    # must not make its unused template newly subject to field validation.
    from src.agent.geometry.parametric_proposal import expand_parametric_instances
    try:
        used = {row['template'] for row in expand_parametric_instances(plan)}
    except (KeyError, TypeError, ValueError):
        # Invalid instance declarations block knowing which templates apply.
        # Keep their formal parser error; still check global opening fields.
        used = set()
    for name, template in templates.items() if isinstance(templates, dict) else ():
        if name not in used or not isinstance(template, dict):
            continue
        base = ('templates', name)
        for index, space in _rows(template, 'spaces'):
            diagnostics.extend(_source_ref_diagnostics(space, (*base, 'spaces', index)))
            if not isinstance(space, dict) or 'role_evidence' not in space:
                continue
            path = (*base, 'spaces', index, 'role_evidence')
            try:
                if 'role' in space and 'id' in space:
                    validated_role_evidence(space)
                else:
                    # Parent fields are checked by the compiler; evidence still
                    # has independent, precisely located schema errors.
                    RoomRoleEvidence.model_validate(space['role_evidence'])
            except ValidationError as error:
                diagnostics.extend(_validation_diagnostics(error, path, RoomRoleEvidence))
            except ValueError as error:
                try:
                    require_role(space['role'])
                except ValueError:
                    diagnostics.append(dict(code='invalid_role',
                        pointer=_pointer((*base, 'spaces', index, 'role')), message=str(error)))
                else:
                    diagnostics.append(dict(code='role_evidence_mismatch',
                        pointer=_pointer((*path, 'role')), message=str(error),
                        related_pointers=[_pointer((*base, 'spaces', index, 'role'))]))
        for field in ('opening_rows', 'doors', 'window_rows'):
            # opening_rows is unsupported in schema 2; retain that compiler error.
            if field == 'opening_rows' and opening_model is WallOpening:
                continue
            for index, row in _rows(template, field):
                path = (*base, field, index)
                if field != 'window_rows':
                    opening_fields(row, path, required_kind=(
                        field == 'opening_rows' and 'kind' in OPENING_ROW_REQUIRED_FIELDS))
                # A window row with no spans emits nothing. The compiler still
                # requires the key, but does not evaluate its value in that case.
                emits = field != 'window_rows' or not isinstance(row, dict) or bool(row.get('spans'))
                diagnostics.extend(_source_ref_diagnostics(row, path, validate_value=emits))
    for field in ('connections', 'core_connections'):
        for index, row in _rows(plan, field):
            if field == 'core_connections' and isinstance(row, dict):
                kind = row.get('kind', 'door')
                state = row.get('state', 'open' if kind == 'open' else 'unknown')
                for name, allowed in core_connection_field_issues(kind, state):
                    diagnostics.append(dict(code='invalid_field', pointer=_pointer((field, index, name)),
                        message='Core connections must be doors or open passages with a valid state',
                        allowed_values=list(allowed)))
            else:
                opening_fields(row, (field, index), required_kind=opening_model.model_fields['kind'].is_required())
            diagnostics.extend(_source_ref_diagnostics(row, (field, index),
                model=opening_model if field == 'connections' else None))
    return diagnostics
