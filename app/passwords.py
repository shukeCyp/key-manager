from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from pwdlib.hashers.bcrypt import BcryptHasher

# New passwords use Argon2; upstream bcrypt hashes remain verifiable.
pwd_context = PasswordHash((Argon2Hasher(), BcryptHasher()))
