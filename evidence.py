"""Encode the CoCo attester's typed SNP report into the signed AMD ABI bytes.

Supports the actual deployment: SNP v5, Turin. Unsupported layouts fail closed.
"""
import base64
import struct


def pack(evidence):
    r = evidence['attestation_report']
    if (r['version'], r['cpuid_fam_id'], r['cpuid_mod_id'], r['cpuid_step']) != (5, 26, 2, 1):
        raise ValueError('Unsupported SNP report layout')
    raw = bytearray(1184)
    def number(at, fmt, value):
        struct.pack_into('<' + fmt, raw, at, value)
    def array(at, value, length):
        if len(value) != length:
            raise ValueError('Invalid report field length')
        raw[at:at+length] = bytes(value)
    for at, key in [(0, 'version'), (4, 'guest_svn'), (0x30, 'vmpl'), (0x34, 'sig_algo'), (0x48, 'key_info')]:
        number(at, 'I', r[key])
    for at, key in [(8, 'policy'), (0x40, 'plat_info'), (0x1f8, 'launch_mit_vector'), (0x200, 'current_mit_vector')]:
        number(at, 'Q', r[key])
    for at, key, n in [(0x10, 'family_id', 16), (0x20, 'image_id', 16), (0x50, 'report_data', 64),
                       (0x90, 'measurement', 48), (0xc0, 'host_data', 32), (0xe0, 'id_key_digest', 48),
                       (0x110, 'author_key_digest', 48), (0x140, 'report_id', 32),
                       (0x160, 'report_id_ma', 32), (0x1a0, 'chip_id', 64)]:
        array(at, r[key], n)
    # Turin has FMC/BL/TEE/SNP in bytes 0..3; microcode in byte 7.
    for at, key in [(0x38, 'current_tcb'), (0x180, 'reported_tcb'), (0x1e0, 'committed_tcb'), (0x1f0, 'launch_tcb')]:
        t = r[key]
        array(at, [t['fmc'], t['bootloader'], t['tee'], t['snp'], 0, 0, 0, t['microcode']], 8)
    array(0x188, [r['cpuid_fam_id'], r['cpuid_mod_id'], r['cpuid_step']], 3)
    for at, key in [(0x1e8, 'current'), (0x1ec, 'committed')]:
        array(at, [r[key]['build'], r[key]['minor'], r[key]['major']], 3)
    array(0x2a0, r['signature']['r'], 72)
    array(0x2e8, r['signature']['s'], 72)
    return bytes(raw)


def b64(value):
    return base64.b64encode(value).decode('ascii')
