# 5. Envelope encryption with workspace-bound associated data

**Decision.** Each customer key is sealed with its own data key; data keys are wrapped by a key in
Key Manager; both seals carry workspace and provider as associated data.

**Why.** The key-encryption key never leaves the KMS, rotation re-wraps small data keys without
touching secrets, and a ciphertext copied into another workspace's row fails to decrypt.
