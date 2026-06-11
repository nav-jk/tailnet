from nacl.public import PrivateKey, PublicKey, Box


def generate_key():
    private_key = PrivateKey.generate()
    public_key = private_key.public_key
    return private_key, public_key


def encrypt(my_private_key: PrivateKey, peer_public_key: PublicKey, msg: bytes) -> bytes:
    box = Box(my_private_key, peer_public_key)
    return box.encrypt(msg)


def decrypt(my_private_key: PrivateKey, peer_public_key: PublicKey, msg: bytes) -> bytes:
    box = Box(my_private_key, peer_public_key)
    return box.decrypt(msg)