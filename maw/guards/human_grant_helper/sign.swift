// human_grant SE signing helper (design §5 / §5a, option A) — CryptoKit build.
//
// The ONE piece of native code in the human_grant design: it holds the operator's grant
// signing key in the Secure Enclave (non-exportable P-256) and signs a grant
// digest under Touch ID. The Python seat side stays pure-stdlib and only
// VERIFIES; this binary only ever runs on the operator's Mac at mint time.
//
// WHY CryptoKit, not the keychain (rewrite, 2026-07-18): the SecKey path
// created the enclave key fine but the kSecAttrIsPermanent keychain ADD fails
// for a CLI binary with errSecMissingEntitlement (-34018) — the data-protection
// keychain wants a keychain-access-groups entitlement only a provisioning
// profile provides (verified live; ad-hoc codesign does not help). The standard
// CLI-tool fix (cf. age-plugin-se): CryptoKit's SecureEnclave API with the key
// persisted as its dataRepresentation — an enclave-ENCRYPTED blob written to a
// plain FILE. No keychain, no entitlement.
//
// Threat model of the blob file (~/.maw/grants/se-key.blob, 0600):
//   - THEFT is useless: the blob is encrypted by THIS Mac's enclave; on any
//     other machine (or without the enclave) it decrypts to nothing.
//   - Plaintext export does not exist: the private scalar never leaves the SE.
//   - Biometry still gates EVERY signature (.biometryCurrentSet — a fingerprint
//     re-enrollment invalidates the key, a free property we keep).
//   - DELETION is a DoS only: rm the blob and grants can't be minted until a
//     re-key + the Layer-1 re-commit ceremony (new .pub PR + ack-keyset). A
//     local FS-write attacker is the same stated ceiling as the ledger/token.
//
// Built from THIS committed source at init/first-use (never a checked-in
// binary):
//     swiftc -O -o human_grant_sign sign.swift
//
// Verbs (output formats unchanged from the keychain build — the Python side
// needs zero changes):
//   human_grant_sign pubkey
//       Create-or-load the SE key and print its public point as "<x>:<y>" hex.
//   human_grant_sign sign <64-hex-char digest> [<reason>]
//       Touch ID (dialog text = reason, S2) -> base64 of the raw 64-byte r||s.
//   human_grant_sign selftest <64-hex-char digest>
//       TEST ONLY: sign with an EPHEMERAL SOFTWARE key through the exact same
//       digest path, print "<x>:<y>" then the base64 sig. Exists so CI can pin
//       the no-double-hash property (the signature must verify under the
//       stdlib guards.p256 against the SAME digest) without enclave hardware
//       or a Touch ID prompt. Never touches the SE or the blob.
//
// Exit non-zero (stdout silent) on any failure — Touch ID refused, no enclave,
// malformed input — so the Python mint fails closed.

import CryptoKit
import Foundation
import LocalAuthentication
import Security

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(1)
}

// The precomputed SHA-256 digest we are handed, as a CryptoKit Digest.
// CryptoKit's DataProtocol signature(for:) overload HASHES its input — signing
// the 32 digest bytes through it would double-hash and verify under nothing.
// Conforming a raw wrapper to Digest selects the sign-this-digest overload, so
// the enclave signs EXACTLY the canonical digest guards.p256 verifies.
struct RawSHA256Digest: Digest {
    static var byteCount: Int { 32 }
    let bytes: [UInt8]

    func withUnsafeBytes<R>(_ body: (UnsafeRawBufferPointer) throws -> R) rethrows -> R {
        try bytes.withUnsafeBytes(body)
    }

    func makeIterator() -> Array<UInt8>.Iterator { bytes.makeIterator() }

    var description: String { "SHA256 digest (raw)" }

    func hash(into hasher: inout Hasher) { hasher.combine(bytes) }

    static func == (lhs: RawSHA256Digest, rhs: RawSHA256Digest) -> Bool {
        lhs.bytes == rhs.bytes
    }
}

func grantsDir() -> URL {
    if let override = ProcessInfo.processInfo.environment["MAW_GRANTS_DIR"] {
        return URL(fileURLWithPath: override)
    }
    return FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".maw").appendingPathComponent("grants")
}

func blobPath() -> URL { grantsDir().appendingPathComponent("se-key.blob") }

func accessControl() -> SecAccessControl {
    var error: Unmanaged<CFError>?
    guard let access = SecAccessControlCreateWithFlags(
        kCFAllocatorDefault,
        kSecAttrAccessibleWhenUnlockedThisDeviceOnly,   // never leaves this Mac
        [.privateKeyUsage, .biometryCurrentSet],        // Touch ID every use
        &error
    ) else { fail("access-control creation failed") }
    return access
}

func loadOrCreateKey(context: LAContext) -> SecureEnclave.P256.Signing.PrivateKey {
    guard SecureEnclave.isAvailable else {
        fail("no Secure Enclave on this machine — mint is Mac-with-SE only")
    }
    let path = blobPath()
    if let blob = try? Data(contentsOf: path) {
        do {
            return try SecureEnclave.P256.Signing.PrivateKey(
                dataRepresentation: blob, authenticationContext: context)
        } catch {
            fail("se-key.blob exists but does not load (\(error)) — a foreign or "
                 + "corrupt blob; resolve by the re-key ceremony, not by overwrite")
        }
    }
    do {
        let key = try SecureEnclave.P256.Signing.PrivateKey(
            accessControl: accessControl(), authenticationContext: context)
        let dir = grantsDir()
        try FileManager.default.createDirectory(
            at: dir, withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700])
        try key.dataRepresentation.write(to: path, options: [.atomic])
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o600], ofItemAtPath: path.path)
        return key
    } catch {
        fail("SE key creation failed: \(error)")
    }
}

// 64-byte x||y -> "<x-hex>:<y-hex>" (the committed .pub format's fields).
func pubkeyLine(_ raw: Data) -> String {
    guard raw.count == 64 else { fail("unexpected public-key length \(raw.count)") }
    let hex = raw.map { String(format: "%02x", $0) }.joined()
    let x = String(hex.prefix(64)), y = String(hex.suffix(64))
    return "\(x):\(y)"
}

let args = CommandLine.arguments
guard args.count >= 2 else {
    fail("usage: human_grant_sign pubkey | sign <hex-digest> [<reason>] | selftest <hex-digest>")
}

switch args[1] {
case "pubkey":
    let ctx = LAContext()
    let key = loadOrCreateKey(context: ctx)
    print(pubkeyLine(key.publicKey.rawRepresentation))

case "sign":
    guard args.count >= 3, args[2].count == 64,
          let digest = Data(hex: args[2]) else { fail("sign needs a 64-hex-char digest") }
    // S2: the Touch ID dialog itself says WHAT is being signed.
    let ctx = LAContext()
    if args.count >= 4 && !args[3].isEmpty {
        ctx.localizedReason = args[3]
    }
    let key = loadOrCreateKey(context: ctx)
    do {
        let sig = try key.signature(for: RawSHA256Digest(bytes: [UInt8](digest)))
        print(sig.rawRepresentation.base64EncodedString())  // 64-byte r||s
    } catch {
        fail("signing failed or was refused: \(error)")
    }

case "selftest":
    // The no-double-hash pin, enclave-free: the SAME RawSHA256Digest path with
    // an ephemeral SOFTWARE key. CI verifies the output under guards.p256
    // against the same digest — if the digest were re-hashed, verification
    // would fail. Never touches the SE or the blob.
    guard args.count == 3, args[2].count == 64,
          let digest = Data(hex: args[2]) else { fail("selftest needs a 64-hex-char digest") }
    let key = P256.Signing.PrivateKey()
    do {
        let sig = try key.signature(for: RawSHA256Digest(bytes: [UInt8](digest)))
        print(pubkeyLine(key.publicKey.rawRepresentation))
        print(sig.rawRepresentation.base64EncodedString())
    } catch {
        fail("selftest signing failed: \(error)")
    }

default:
    fail("unknown verb \(args[1])")
}

extension Data {
    init?(hex: String) {
        guard hex.count % 2 == 0 else { return nil }
        var d = Data(capacity: hex.count / 2)
        var idx = hex.startIndex
        while idx < hex.endIndex {
            let next = hex.index(idx, offsetBy: 2)
            guard let b = UInt8(hex[idx..<next], radix: 16) else { return nil }
            d.append(b)
            idx = next
        }
        self = d
    }
}
