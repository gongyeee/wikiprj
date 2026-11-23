"""File intake: read server sources or receive a browser folder, then publish editions."""
import hashlib
import uuid
import shutil
import sqlite3
from pathlib import Path, PurePosixPath

from django.conf import settings

from .document_versions import read_catalog
from .file_paths import fs_path
from .search_index import project_raw_dir, SUPPORTED_RAW_SUFFIXES, connect_index


def upload_target(project):
    """A migrated registration must never make a browser upload use a dead path."""
    root = Path(project['path']).resolve()
    raw = project_raw_dir(project)
    target = dict(project)
    if not fs_path(root).is_dir():
        root = (Path(settings.BASE_DIR) / 'data' / 'upload-projects' / str(project['id'])).resolve()
        raw = root / 'raw' / 'sources'
        target.update(path=str(root), rawDir=str(raw), exportDir=str(root / 'exports' / 'wiki'),
                      originalProjectPath=project['path'],
                      storageNotice='项目的旧目录在当前服务器不可用，文件已保存到当前服务器；摄入成功后才更新项目位置。')
    elif not fs_path(raw).is_dir():
        raw = root / 'raw' / 'sources'
        target.update(rawDir=str(raw), storageNotice='旧原文目录不可用，上传文件将保存到当前项目的原文目录。')
    return target


def scan_ingestion_sources(project, selected_refs=None):
    raw = project_raw_dir(project)
    walk_root = fs_path(raw)
    if not walk_root.is_dir():
        raise ValueError(f'服务端本地目录不存在或当前服务无法访问：{raw}')
    catalog = read_catalog(project)
    known_files = {}
    connection = connect_index(project)
    if connection is not None:
        try:
            known_files = {row['relative_path']: row for row in connection.execute('SELECT relative_path,size,mtime_ns FROM documents')}
        except sqlite3.OperationalError:
            pass
        finally:
            connection.close()
    groups = {row['logical_id']: {'id': row['logical_id'], 'name': row['name']} for row in catalog}
    files, unsupported = [], []
    candidates = (walk_root / ref for ref in sorted(selected_refs)) if selected_refs is not None else sorted(walk_root.rglob('*'))
    for path in candidates:
        ref = path.relative_to(walk_root).as_posix()
        if (not path.is_file() or path.name.startswith('~$')
                or any(part in {'.index', 'document-history'} for part in path.relative_to(walk_root).parts)
                or selected_refs is not None and ref not in selected_refs):
            continue
        if path.suffix.lower() not in SUPPORTED_RAW_SUFFIXES:
            unsupported.append({'sourceFile': ref})
            continue
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        previous = [row for row in catalog if row['source_ref'] == ref]
        exact = next((row for row in previous if row['file_hash'] == digest), None)
        group = exact or (previous[0] if previous else None)
        known = known_files.get(ref)
        stat = path.stat()
        unchanged_legacy = not previous and known and known['size'] == stat.st_size and known['mtime_ns'] == stat.st_mtime_ns
        display = '/'.join(ref.split('/')[2:]) if ref.startswith('上传文件/') else ref
        files.append({'sourceFile': ref, 'displayName': display, 'fileHash': digest,
                      'logicalDocumentId': group['logical_id'] if group else '',
                      'documentName': group['name'] if group else path.stem,
                      'versionLabel': exact['label'] if exact else path.stem,
                      'editionId': exact['uid'] if exact else '',
                      'status': 'indexed' if exact or unchanged_legacy else 'changed' if previous or known else 'new'})
    files.sort(key=lambda row: (row['displayName'].casefold(), row['sourceFile']))
    return {'files': files, 'unsupported': unsupported, 'documents': list(groups.values()), 'rawDir': str(raw)}


def normalize_assignments(values):
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 1000:
        raise ValueError('文档归属必须是列表，单次最多1000份文件')
    result, seen = [], set()
    for value in values:
        if not isinstance(value, dict):
            raise ValueError('文档归属格式错误')
        item = {}
        for key, limit in [('sourceFile', 2000), ('logicalDocumentId', 100), ('documentName', 200), ('versionLabel', 100)]:
            text = value.get(key, '')
            if not isinstance(text, str) or len(text) > limit:
                raise ValueError('文件、文档名称或版本号格式错误')
            item[key] = text.strip()
        ref = item['sourceFile'].replace('\\', '/')
        if not ref or ref.startswith('/') or ':' in ref or '..' in ref.split('/') or ref in seen:
            raise ValueError('文件路径无效或重复')
        if not item['versionLabel'] or bool(item['logicalDocumentId']) == bool(item['documentName']):
            raise ValueError('请选择所属文档并填写版本号')
        item['sourceFile'] = ref
        item['setDefault'] = value.get('setDefault', False)
        if type(item['setDefault']) is not bool:
            raise ValueError('默认版选项须为布尔值')
        seen.add(ref)
        result.append(item)
    return result


def plan_assignments(rows, assignments, source_uids):
    rows = {row['uid']: dict(row) for row in rows}
    names = {row['logical_id']: row['name'] for row in rows.values()}
    old_defaults = {row['uid']: row['logical_id'] for row in rows.values() if row['is_default']}
    defaults, first_choices = {}, {}
    for item in assignments:
        uid = source_uids.get(item['sourceFile'])
        if uid not in rows:
            raise ValueError(f"文件不在本次摄入范围内：{item['sourceFile']}")
        group = item['logicalDocumentId']
        if group and group not in names:
            raise ValueError('所属文档不存在，请刷新列表')
        if not group:
            matches = [key for key, name in names.items() if name == item['documentName']]
            if len(matches) > 1:
                raise ValueError('存在多个同名文档，请选择文档ID')
            group = matches[0] if matches else uuid.uuid4().hex
            names[group] = item['documentName']
        first_choices.setdefault(group, uid)
        rows[uid].update(logical_id=group, name=names[group], label=item['versionLabel'])
        if item['setDefault']:
            if group in defaults:
                raise ValueError('同一文档只能指定一个默认版本')
            defaults[group] = uid
    labels, groups = set(), {}
    for row in rows.values():
        key = (row['logical_id'], row['label'])
        if key in labels:
            raise ValueError(f"同一文档的版本号不能重复：{row['name']} · {row['label']}")
        labels.add(key)
        groups.setdefault(row['logical_id'], []).append(row)
    for group, editions in groups.items():
        chosen = defaults.get(group) or next((row['uid'] for row in editions if old_defaults.get(row['uid']) == group), first_choices.get(group, editions[0]['uid']))
        for row in editions:
            row.update(name=names[group], is_default=int(row['uid'] == chosen))
    return list(rows.values())


def validate_source_assignments(project, values, source_files=None, limit_docs=0):
    items = normalize_assignments(values)
    if not items:
        return []
    files = scan_ingestion_sources(project)['files']
    if source_files:
        files = [row for row in files if row['sourceFile'] in {ref.replace('\\', '/') for ref in source_files}]
    elif limit_docs:
        files = files[:limit_docs]
    rows, source_uids = read_catalog(project), {}
    for row in files:
        uid = row['editionId'] or f"pending:{row['sourceFile']}"
        source_uids[row['sourceFile']] = uid
        if not row['editionId']:
            rows.append({'uid': uid, 'logical_id': row['logicalDocumentId'] or uid, 'name': row['documentName'],
                         'label': uid, 'is_default': int(not row['logicalDocumentId'])})
    plan_assignments(rows, items, source_uids)
    return items


def apply_source_assignments(connection, values):
    from .search_index import get_meta
    items = normalize_assignments(values)
    if not items:
        return
    rows = [dict(row) for row in connection.execute('SELECT * FROM document_editions ORDER BY document_id')]
    imported = set(get_meta(connection, 'document_import_documents', []))
    source_uids = {row['source_ref']: row['uid'] for row in rows if row['document_id'] in imported}
    planned = plan_assignments(rows, items, source_uids)
    for item in items:
        if item['setDefault']:
            row = next(row for row in rows if row['uid'] == source_uids[item['sourceFile']])
            if not connection.execute('SELECT 1 FROM chunks WHERE document_id=?', (row['document_id'],)).fetchone():
                raise ValueError('空版本不能设为默认')
    for row in rows:
        connection.execute('UPDATE document_editions SET label=?,is_default=0 WHERE uid=?', (uuid.uuid4().hex, row['uid']))
    for row in planned:
        connection.execute('UPDATE document_editions SET logical_id=?,name=?,label=?,is_default=? WHERE uid=?',
                           (row['logical_id'], row['name'], row['label'], row['is_default'], row['uid']))


def receive_upload(project, uploads, paths):
    if not uploads or len(uploads) != len(paths) or len(uploads) > 1000:
        raise ValueError('请选择文件或目录，单次最多1000份文件')
    safe_paths, seen = [], set()
    for ref, upload in zip(paths, uploads):
        if not isinstance(ref, str) or len(ref) > 1500:
            raise ValueError('上传文件路径无效')
        ref = ref.replace('\\', '/')
        parts = ref.split('/')
        if (not ref or ref.startswith('/') or ':' in ref or any(part in {'', '.', '..'} or part.endswith(('.', ' ')) for part in parts)
                or any(any(char in part for char in '<>"|?*\x00') for part in parts)
                or any(part.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *[f'COM{i}' for i in range(1,10)], *[f'LPT{i}' for i in range(1,10)]} for part in parts)
                or ref.casefold() in seen):
            raise ValueError('上传路径无效或重复')
        if PurePosixPath(ref).suffix.lower() not in SUPPORTED_RAW_SUFFIXES:
            raise ValueError(f'暂不支持这个文件类型：{ref}')
        safe_paths.append(ref)
        seen.add(ref.casefold())
    if sum(upload.size for upload in uploads) > 512 * 1024 * 1024:
        raise ValueError('单次上传总大小不能超过512MB')
    raw = project_raw_dir(project)
    staging = Path(settings.BASE_DIR) / 'data' / 'upload-staging' / uuid.uuid4().hex
    staging.mkdir(parents=True)
    created, refs = [], []
    try:
        for index, (upload, ref) in enumerate(zip(uploads, safe_paths)):
            temporary = staging / str(index)
            digest = hashlib.sha256()
            with temporary.open('wb') as stream:
                for chunk in upload.chunks():
                    digest.update(chunk)
                    stream.write(chunk)
            relative = f'上传文件/{digest.hexdigest()}/{ref}'
            target = (raw / relative).resolve()
            if not target.is_relative_to(raw.resolve()):
                raise ValueError('上传路径超出原文目录')
            target = fs_path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest.hexdigest():
                    raise ValueError('已存在的上传文件被修改，请检查原文目录')
            else:
                # Exclusive create never overwrites a user's source or another upload.
                with target.open('xb') as stream, temporary.open('rb') as source:
                    created.append(target)
                    shutil.copyfileobj(source, stream)
            refs.append(relative)
        result = scan_ingestion_sources(project, set(refs))
        result['receivedFiles'] = len(refs)
        return result
    except Exception:
        for target in created:
            target.unlink(missing_ok=True)
        raise
    finally:
        for child in staging.iterdir():
            child.unlink()
        staging.rmdir()
