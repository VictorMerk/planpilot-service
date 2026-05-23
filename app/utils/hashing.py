import hashlib


def compute_hash_from_files(domain_file_bytes: bytes, problem_file_bytes: bytes) -> str:
    hasher = hashlib.sha256()
    hasher.update(domain_file_bytes)
    hasher.update(problem_file_bytes)
    return hasher.hexdigest()
