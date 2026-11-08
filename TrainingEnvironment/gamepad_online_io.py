"""Bounded JSON parsing/writing so the native management lease keeps advancing."""
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from ModelTrainer.common import read as common_read


def same_bindings(left, right):
    """Compare pinned contents using native filesystem path identity.

    Windows account spelling may change after restore. Hashes must still match;
    reject duplicate path aliases instead of allowing one to hide another.
    """
    def canonical(mapping):
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError('Empty or invalid checkpoint binding')
        result = {}
        for name, expected in mapping.items():
            path = Path(name).resolve()
            if path in result:
                raise ValueError('Duplicate checkpoint binding path')
            result[path] = expected
        return result
    return canonical(left) == canonical(right)


def read(path):
    path=Path(path)
    with path.open(encoding='utf-8') as f:
        first=f.read(1)
        if first!='[':return common_read(path)
        decoder=json.JSONDecoder();buffer='';result=[];eof=False
        while True:
            buffer=buffer.lstrip()
            if buffer.startswith(','):buffer=buffer[1:].lstrip()
            if buffer.startswith(']'):return result
            try:
                value,end=decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                if eof:raise ValueError('Incomplete JSON array: '+str(path))
                chunk=f.read(262144);eof=not chunk;buffer+=chunk;continue
            result.append(value);buffer=buffer[end:]
            if len(result)%512==0:time.sleep(0)


def save(path,value,replace=False):
    path=Path(path)
    if path.exists() and not replace:raise FileExistsError(path)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temp.open('x',encoding='utf-8') as f:
            if isinstance(value,list):
                f.write('[')
                for i,row in enumerate(value):
                    if i:f.write(',\n')
                    f.write(json.dumps(row,ensure_ascii=False,allow_nan=False))
                    if i%512==0:time.sleep(0)
                f.write(']\n')
            else:f.write(json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2)+'\n')
            f.flush();os.fsync(f.fileno())
        if replace:os.replace(temp,path)
        else:
            # Publish a complete inode atomically, without clobbering an existing
            # immutable request. The supervisor polls the final pathname.
            os.link(temp,path)
    finally:
        if temp.exists():temp.unlink()


def rows_digest(rows):
    h=hashlib.sha256();h.update(b'[')
    for i,row in enumerate(rows):
        if i:h.update(b',')
        h.update(json.dumps(row,sort_keys=True,separators=(',',':')).encode())
        if i%512==0:time.sleep(0)
    h.update(b']');return h.hexdigest()
