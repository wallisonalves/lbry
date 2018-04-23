from itertools import chain
from binascii import hexlify
from collections import namedtuple

from .bcd_data_stream import BCDataStream
from .util import subclass_tuple

# bitcoin opcodes
OP_0 = 0x00
OP_1 = 0x51
OP_16 = 0x60
OP_DUP = 0x76
OP_HASH160 = 0xa9
OP_EQUALVERIFY = 0x88
OP_CHECKSIG = 0xac
OP_CHECKMULTISIG = 0xae
OP_EQUAL = 0x87
OP_PUSHDATA1 = 0x4c
OP_PUSHDATA2 = 0x4d
OP_PUSHDATA4 = 0x4e
OP_2DROP = 0x6d
OP_DROP = 0x75

# lbry custom opcodes
OP_CLAIM_NAME = 0xb5
OP_SUPPORT_CLAIM = 0xb6
OP_UPDATE_CLAIM = 0xb7


# template matching opcodes (not real opcodes)
# base class for PUSH_DATA related opcodes
PUSH_DATA_OP = namedtuple('PUSH_DATA_OP', 'name')
# opcode for variable length strings
PUSH_SINGLE = subclass_tuple('PUSH_SINGLE', PUSH_DATA_OP)
# opcode for variable number of variable length strings
PUSH_MANY = subclass_tuple('PUSH_MANY', PUSH_DATA_OP)
# opcode with embedded subscript parsing
PUSH_SUBSCRIPT = namedtuple('PUSH_SUBSCRIPT', 'name template')


def is_push_data_opcode(opcode):
    return isinstance(opcode, PUSH_DATA_OP) or isinstance(opcode, PUSH_SUBSCRIPT)


def is_push_data_token(token):
    return 1 <= token <= OP_PUSHDATA4


def push_data(data):
    size = len(data)
    if size < OP_PUSHDATA1:
        yield BCDataStream.uint8.pack(size)
    elif size <= 0xFF:
        yield BCDataStream.uint8.pack(OP_PUSHDATA1)
        yield BCDataStream.uint8.pack(size)
    elif size <= 0xFFFF:
        yield BCDataStream.uint8.pack(OP_PUSHDATA2)
        yield BCDataStream.uint16.pack(size)
    else:
        yield BCDataStream.uint8.pack(OP_PUSHDATA4)
        yield BCDataStream.uint32.pack(size)
    yield data


def read_data(token, stream):
    if token < OP_PUSHDATA1:
        return stream.read(token)
    elif token == OP_PUSHDATA1:
        return stream.read(stream.read_uint8())
    elif token == OP_PUSHDATA2:
        return stream.read(stream.read_uint16())
    else:
        return stream.read(stream.read_uint32())


# opcode for OP_1 - OP_16
SMALL_INTEGER = namedtuple('SMALL_INTEGER', 'name')


def is_small_integer(token):
    return OP_1 <= token <= OP_16


def push_small_integer(num):
    assert 1 <= num <= 16
    yield BCDataStream.uint8.pack(OP_1 + (num - 1))


def read_small_integer(token):
    return (token - OP_1) + 1


class Token(namedtuple('Token', 'value')):
    __slots__ = ()

    def __repr__(self):
        name = None
        for var_name, var_value in globals().items():
            if var_name.startswith('OP_') and var_value == self.value:
                name = var_name
                break
        return name or self.value


class DataToken(Token):
    __slots__ = ()

    def __repr__(self):
        return '"{}"'.format(hexlify(self.value))


class SmallIntegerToken(Token):
    __slots__ = ()

    def __repr__(self):
        return 'SmallIntegerToken({})'.format(self.value)


def token_producer(source):
    token = source.read_uint8()
    while token is not None:
        if is_push_data_token(token):
            yield DataToken(read_data(token, source))
        elif is_small_integer(token):
            yield SmallIntegerToken(read_small_integer(token))
        else:
            yield Token(token)
        token = source.read_uint8()


def tokenize(source):
    return list(token_producer(source))


class ScriptError(Exception):
    """ General script handling error. """


class ParseError(ScriptError):
    """ Script parsing error. """


class Parser:

    def __init__(self, opcodes, tokens):
        self.opcodes = opcodes
        self.tokens = tokens
        self.values = {}
        self.token_index = 0
        self.opcode_index = 0

    def parse(self):
        while self.token_index < len(self.tokens) and self.opcode_index < len(self.opcodes):
            token = self.tokens[self.token_index]
            opcode = self.opcodes[self.opcode_index]
            if isinstance(token, DataToken):
                if isinstance(opcode, (PUSH_SINGLE, PUSH_SUBSCRIPT)):
                    self.push_single(opcode, token.value)
                elif isinstance(opcode, PUSH_MANY):
                    self.consume_many_non_greedy()
                else:
                    raise ParseError("DataToken found but opcode was '{}'.".format(opcode))
            elif isinstance(token, SmallIntegerToken):
                if isinstance(opcode, SMALL_INTEGER):
                    self.values[opcode.name] = token.value
                else:
                    raise ParseError("SmallIntegerToken found but opcode was '{}'.".format(opcode))
            elif token.value == opcode:
                pass
            else:
                raise ParseError("Token is '{}' and opcode is '{}'.".format(token.value, opcode))
            self.token_index += 1
            self.opcode_index += 1

        if self.token_index < len(self.tokens):
            raise ParseError("Parse completed without all tokens being consumed.")

        if self.opcode_index < len(self.opcodes):
            raise ParseError("Parse completed without all opcodes being consumed.")

        return self

    def consume_many_non_greedy(self):
        """ Allows PUSH_MANY to consume data without being greedy
            in cases when one or more PUSH_SINGLEs follow a PUSH_MANY. This will
            prioritize giving all PUSH_SINGLEs some data and only after that
            subsume the rest into PUSH_MANY.
        """

        token_values = []
        while self.token_index < len(self.tokens):
            token = self.tokens[self.token_index]
            if not isinstance(token, DataToken):
                self.token_index -= 1
                break
            token_values.append(token.value)
            self.token_index += 1

        push_opcodes = []
        push_many_count = 0
        while self.opcode_index < len(self.opcodes):
            opcode = self.opcodes[self.opcode_index]
            if not is_push_data_opcode(opcode):
                self.opcode_index -= 1
                break
            if isinstance(opcode, PUSH_MANY):
                push_many_count += 1
            push_opcodes.append(opcode)
            self.opcode_index += 1

        if push_many_count > 1:
            raise ParseError(
                "Cannot have more than one consecutive PUSH_MANY, as there is no way to tell which"
                " token value should go into which PUSH_MANY."
            )

        if len(push_opcodes) > len(token_values):
            raise ParseError(
                "Not enough token values to match all of the PUSH_MANY and PUSH_SINGLE opcodes."
            )

        many_opcode = push_opcodes.pop(0)

        # consume data into PUSH_SINGLE opcodes, working backwards
        for opcode in reversed(push_opcodes):
            self.push_single(opcode, token_values.pop())

        # finally PUSH_MANY gets everything that's left
        self.values[many_opcode.name] = token_values

    def push_single(self, opcode, value):
        if isinstance(opcode, PUSH_SINGLE):
            self.values[opcode.name] = value
        elif isinstance(opcode, PUSH_SUBSCRIPT):
            self.values[opcode.name] = Script.from_source_with_template(value, opcode.template)
        else:
            raise ParseError("Not a push single or subscript: {}".format(opcode))


class Template(object):

    __slots__ = 'name', 'opcodes'

    def __init__(self, name, opcodes):
        self.name = name
        self.opcodes = opcodes

    def parse(self, tokens):
        return Parser(self.opcodes, tokens).parse().values

    def generate(self, values):
        source = BCDataStream()
        for opcode in self.opcodes:
            if isinstance(opcode, PUSH_SINGLE):
                data = values[opcode.name]
                source.write_many(push_data(data))
            elif isinstance(opcode, PUSH_SUBSCRIPT):
                data = values[opcode.name]
                source.write_many(push_data(data.source))
            elif isinstance(opcode, PUSH_MANY):
                for data in values[opcode.name]:
                    source.write_many(push_data(data))
            elif isinstance(opcode, SMALL_INTEGER):
                data = values[opcode.name]
                source.write_many(push_small_integer(data))
            else:
                source.write_uint8(opcode)
        return source.get_bytes()


class Script(object):

    __slots__ = 'source', 'template', 'values'

    templates = []

    def __init__(self, source=None, template=None, values=None, template_hint=None):
        self.source = source
        self.template = template
        self.values = values
        if source:
            self.parse(template_hint)
        elif template and values:
            self.generate()

    @property
    def tokens(self):
        return tokenize(BCDataStream(self.source))

    @classmethod
    def from_source_with_template(cls, source, template):
        if template in InputScript.templates:
            return InputScript(source, template_hint=template)
        elif template in OutputScript.templates:
            return OutputScript(source, template_hint=template)
        else:
            return cls(source, template_hint=template)

    def parse(self, template_hint=None):
        tokens = self.tokens
        for template in chain((template_hint,), self.templates):
            if not template:
                continue
            try:
                self.values = template.parse(tokens)
                self.template = template
                return
            except ParseError:
                continue
        raise ValueError('No matching templates for source: {}'.format(hexlify(self.source)))

    def generate(self):
        self.source = self.template.generate(self.values)


class InputScript(Script):
    """ Input / redeem script templates (aka scriptSig) """

    __slots__ = ()

    REDEEM_PUBKEY = Template('pubkey', (
        PUSH_SINGLE('signature'),
    ))
    REDEEM_PUBKEY_HASH = Template('pubkey_hash', (
        PUSH_SINGLE('signature'), PUSH_SINGLE('pubkey')
    ))
    REDEEM_SCRIPT = Template('script', (
        SMALL_INTEGER('signatures_count'), PUSH_MANY('pubkeys'), SMALL_INTEGER('pubkeys_count'),
        OP_CHECKMULTISIG
    ))
    REDEEM_SCRIPT_HASH = Template('script_hash', (
        OP_0, PUSH_MANY('signatures'), PUSH_SUBSCRIPT('script', REDEEM_SCRIPT)
    ))

    templates = [
        REDEEM_PUBKEY,
        REDEEM_PUBKEY_HASH,
        REDEEM_SCRIPT_HASH,
        REDEEM_SCRIPT
    ]

    @classmethod
    def redeem_pubkey_hash(cls, signature, pubkey):
        return cls(template=cls.REDEEM_PUBKEY_HASH, values={
            'signature': signature,
            'pubkey': pubkey
        })

    @classmethod
    def redeem_script_hash(cls, signatures, pubkeys):
        return cls(template=cls.REDEEM_SCRIPT_HASH, values={
            'signatures': signatures,
            'script': cls.redeem_script(signatures, pubkeys)
        })

    @classmethod
    def redeem_script(cls, signatures, pubkeys):
        return cls(template=cls.REDEEM_SCRIPT, values={
            'signatures_count': len(signatures),
            'pubkeys': pubkeys,
            'pubkeys_count': len(pubkeys)
        })


class OutputScript(Script):

    __slots__ = ()

    # output / payment script templates (aka scriptPubKey)
    PAY_PUBKEY_HASH = Template('pay_pubkey_hash', (
        OP_DUP, OP_HASH160, PUSH_SINGLE('pubkey_hash'), OP_EQUALVERIFY, OP_CHECKSIG
    ))
    PAY_SCRIPT_HASH = Template('pay_script_hash', (
        OP_HASH160, PUSH_SINGLE('script_hash'), OP_EQUAL
    ))

    CLAIM_NAME_OPCODES = (
        OP_CLAIM_NAME, PUSH_SINGLE('claim_name'), PUSH_SINGLE('claim'),
        OP_2DROP, OP_DROP
    )
    CLAIM_NAME_PUBKEY = Template('claim_name+pay_pubkey_hash', (
        CLAIM_NAME_OPCODES + PAY_PUBKEY_HASH.opcodes
    ))
    CLAIM_NAME_SCRIPT = Template('claim_name+pay_script_hash', (
        CLAIM_NAME_OPCODES + PAY_SCRIPT_HASH.opcodes
    ))

    SUPPORT_CLAIM_OPCODES = (
        OP_SUPPORT_CLAIM, PUSH_SINGLE('claim_name'), PUSH_SINGLE('claim_id'),
        OP_2DROP, OP_DROP
    )
    SUPPORT_CLAIM_PUBKEY = Template('support_claim+pay_pubkey_hash', (
        SUPPORT_CLAIM_OPCODES + PAY_PUBKEY_HASH.opcodes
    ))
    SUPPORT_CLAIM_SCRIPT = Template('support_claim+pay_script_hash', (
            SUPPORT_CLAIM_OPCODES + PAY_SCRIPT_HASH.opcodes
    ))

    UPDATE_CLAIM_OPCODES = (
        OP_UPDATE_CLAIM, PUSH_SINGLE('claim_name'), PUSH_SINGLE('claim_id'), PUSH_SINGLE('claim'),
        OP_2DROP, OP_2DROP
    )
    UPDATE_CLAIM_PUBKEY = Template('update_claim+pay_pubkey_hash', (
            UPDATE_CLAIM_OPCODES + PAY_PUBKEY_HASH.opcodes
    ))
    UPDATE_CLAIM_SCRIPT = Template('update_claim+pay_script_hash', (
            UPDATE_CLAIM_OPCODES + PAY_SCRIPT_HASH.opcodes
    ))

    templates = [
        PAY_PUBKEY_HASH,
        PAY_SCRIPT_HASH,
        CLAIM_NAME_PUBKEY,
        CLAIM_NAME_SCRIPT,
        SUPPORT_CLAIM_PUBKEY,
        SUPPORT_CLAIM_SCRIPT,
        UPDATE_CLAIM_PUBKEY,
        UPDATE_CLAIM_SCRIPT
    ]

    @classmethod
    def pay_pubkey_hash(cls, pubkey_hash):
        return cls(template=cls.PAY_PUBKEY_HASH, values={
            'pubkey_hash': pubkey_hash
        })

    @classmethod
    def pay_script_hash(cls, script_hash):
        return cls(template=cls.PAY_SCRIPT_HASH, values={
            'script_hash': script_hash
        })

    @classmethod
    def pay_claim_name_pubkey_hash(cls, claim_name, claim, pubkey_hash):
        return cls(template=cls.CLAIM_NAME_PUBKEY, values={
            'claim_name': claim_name,
            'claim': claim,
            'pubkey_hash': pubkey_hash
        })

    @property
    def is_pay_pubkey_hash(self):
        return self.template.name.endswith('pay_pubkey_hash')

    @property
    def is_pay_script_hash(self):
        return self.template.name.endswith('pay_script_hash')

    @property
    def is_claim_name(self):
        return self.template.name.startswith('claim_name+')

    @property
    def is_support_claim(self):
        return self.template.name.startswith('support_claim+')

    @property
    def is_update_claim(self):
        return self.template.name.startswith('update_claim+')

    @property
    def is_claim_involved(self):
        return self.is_claim_name or self.is_support_claim or self.is_update_claim