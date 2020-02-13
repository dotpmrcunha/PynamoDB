"""
PynamoDB attributes
"""
import calendar
import six
from six import add_metaclass
import json
import time
from base64 import b64encode, b64decode
from copy import deepcopy
from datetime import datetime, timedelta
import warnings
from dateutil.parser import parse
from dateutil.tz import tzutc
from inspect import getmembers
from pynamodb.constants import (
    STRING, STRING_SHORT, NUMBER, BINARY, UTC, DATETIME_FORMAT, BINARY_SET, STRING_SET, NUMBER_SET,
    MAP, MAP_SHORT, LIST, LIST_SHORT, DEFAULT_ENCODING, BOOLEAN, ATTR_TYPE_MAP, NUMBER_SHORT, NULL, SHORT_ATTR_TYPES
)
from pynamodb.expressions.operand import Path
from pynamodb._compat import getfullargspec
import collections


class Attribute(object):
    """
    An attribute of a model
    """
    attr_type = None
    null = False

    def __init__(self,
                 hash_key=False,
                 range_key=False,
                 null=None,
                 default=None,
                 default_for_new=None,
                 attr_name=None
                 ):
        if default and default_for_new:
            raise ValueError("An attribute cannot have both default and default_for_new parameters")
        self.default = default
        # This default is only set for new objects (ie: it's not set for re-saved objects)
        self.default_for_new = default_for_new

        if null is not None:
            self.null = null
        self.is_hash_key = hash_key
        self.is_range_key = range_key
        self.attr_path = [attr_name]

    @property
    def attr_name(self):
        return self.attr_path[-1]

    @attr_name.setter
    def attr_name(self, value):
        self.attr_path[-1] = value

    def __set__(self, instance, value):
        if instance and not self._is_map_attribute_class_object(instance):
            attr_name = instance._dynamo_to_python_attrs.get(self.attr_name, self.attr_name)
            instance.attribute_values[attr_name] = value

    def __get__(self, instance, owner):
        if self._is_map_attribute_class_object(instance):
            # MapAttribute class objects store a local copy of the attribute with `attr_path` set to the document path.
            attr_name = instance._dynamo_to_python_attrs.get(self.attr_name, self.attr_name)
            return instance.__dict__.get(attr_name, None) or self
        elif instance:
            attr_name = instance._dynamo_to_python_attrs.get(self.attr_name, self.attr_name)
            return instance.attribute_values.get(attr_name, None)
        else:
            return self

    def _is_map_attribute_class_object(self, instance):
        return isinstance(instance, MapAttribute) and not instance._is_attribute_container()

    def serialize(self, value):
        """
        This method should return a dynamodb compatible value
        """
        return value

    def deserialize(self, value):
        """
        Performs any needed deserialization on the value
        """
        return value

    def get_value(self, value):
        serialized_dynamo_type = ATTR_TYPE_MAP[self.attr_type]
        return value.get(serialized_dynamo_type)

    def __iter__(self):
        # Because we define __getitem__ below for condition expression support
        raise TypeError("'{}' object is not iterable".format(self.__class__.__name__))

    # Condition Expression Support
    def __eq__(self, other):
        if isinstance(other, MapAttribute) and other._is_attribute_container():
            return Path(self).__eq__(other)
        if other is None or isinstance(other, Attribute):  # handle object identity comparison
            return self is other
        return Path(self).__eq__(other)

    def __ne__(self, other):
        if isinstance(other, MapAttribute) and other._is_attribute_container():
            return Path(self).__ne__(other)
        if other is None or isinstance(other, Attribute):  # handle object identity comparison
            return self is not other
        return Path(self).__ne__(other)

    def __lt__(self, other):
        return Path(self).__lt__(other)

    def __le__(self, other):
        return Path(self).__le__(other)

    def __gt__(self, other):
        return Path(self).__gt__(other)

    def __ge__(self, other):
        return Path(self).__ge__(other)

    def __getitem__(self, idx):
        return Path(self).__getitem__(idx)

    def between(self, lower, upper):
        return Path(self).between(lower, upper)

    def is_in(self, *values):
        return Path(self).is_in(*values)

    def exists(self):
        return Path(self).exists()

    def does_not_exist(self):
        return Path(self).does_not_exist()

    def is_type(self):
        # What makes sense here? Are we using this to check if deserialization will be successful?
        return Path(self).is_type(ATTR_TYPE_MAP[self.attr_type])

    def startswith(self, prefix):
        return Path(self).startswith(prefix)

    def contains(self, item):
        return Path(self).contains(item)

    # Update Expression Support
    def __add__(self, other):
        return Path(self).__add__(other)

    def __radd__(self, other):
        return Path(self).__radd__(other)

    def __sub__(self, other):
        return Path(self).__sub__(other)

    def __rsub__(self, other):
        return Path(self).__rsub__(other)

    def __or__(self, other):
        return Path(self).__or__(other)

    def append(self, other):
        return Path(self).append(other)

    def prepend(self, other):
        return Path(self).prepend(other)

    def set(self, value):
        return Path(self).set(value)

    def remove(self):
        return Path(self).remove()

    def add(self, *values):
        return Path(self).add(*values)

    def delete(self, *values):
        return Path(self).delete(*values)


class AttributeContainerMeta(type):

    def __init__(cls, name, bases, attrs):
        super(AttributeContainerMeta, cls).__init__(name, bases, attrs)
        AttributeContainerMeta._initialize_attributes(cls)

    @staticmethod
    def _initialize_attributes(cls):
        """
        Initialize attributes on the class.
        """
        cls._attributes = {}
        cls._dynamo_to_python_attrs = {}

        for name, attribute in getmembers(cls, lambda o: isinstance(o, Attribute)):
            initialized = False
            if isinstance(attribute, MapAttribute):
                # MapAttribute instances that are class attributes of an AttributeContainer class
                # should behave like an Attribute instance and not an AttributeContainer instance.
                initialized = attribute._make_attribute()

            cls._attributes[name] = attribute
            if attribute.attr_name is not None:
                cls._dynamo_to_python_attrs[attribute.attr_name] = name
            else:
                attribute.attr_name = name

            if initialized and isinstance(attribute, MapAttribute):
                # To support creating expressions from nested attributes, MapAttribute instances
                # store local copies of the attributes in cls._attributes with `attr_path` set.
                # Prepend the `attr_path` lists with the dynamo attribute name.
                attribute._update_attribute_paths(attribute.attr_name)


@add_metaclass(AttributeContainerMeta)
class AttributeContainer(object):

    def __init__(self, _user_instantiated=True, **attributes):
        # The `attribute_values` dictionary is used by the Attribute data descriptors in cls._attributes
        # to store the values that are bound to this instance. Attributes store values in the dictionary
        # using the `python_attr_name` as the dictionary key. "Raw" (i.e. non-subclassed) MapAttribute
        # instances do not have any Attributes defined and instead use this dictionary to store their
        # collection of name-value pairs.
        self.attribute_values = {}
        self._set_defaults(_user_instantiated=_user_instantiated)
        self._set_attributes(**attributes)

    @classmethod
    def _get_attributes(cls):
        """
        Returns the attributes of this class as a mapping from `python_attr_name` => `attribute`.

        :rtype: dict[str, Attribute]
        """
        warnings.warn("`Model._get_attributes` is deprecated in favor of `Model.get_attributes` now")
        return cls.get_attributes()

    @classmethod
    def get_attributes(cls):
        """
        Returns the attributes of this class as a mapping from `python_attr_name` => `attribute`.

        :rtype: dict[str, Attribute]
        """
        return cls._attributes

    @classmethod
    def _dynamo_to_python_attr(cls, dynamo_key):
        """
        Convert a DynamoDB attribute name to the internal Python name.

        This covers cases where an attribute name has been overridden via "attr_name".
        """
        return cls._dynamo_to_python_attrs.get(dynamo_key, dynamo_key)

    def _set_defaults(self, _user_instantiated=True):
        """
        Sets and fields that provide a default value
        """
        for name, attr in self.get_attributes().items():
            if _user_instantiated and attr.default_for_new is not None:
                default = attr.default_for_new
            else:
                default = attr.default
            if callable(default):
                value = default()
            else:
                value = default
            if value is not None:
                setattr(self, name, value)

    def _set_attributes(self, **attributes):
        """
        Sets the attributes for this object
        """
        for attr_name, attr_value in six.iteritems(attributes):
            if attr_name not in self.get_attributes():
                raise ValueError("Attribute {} specified does not exist".format(attr_name))
            setattr(self, attr_name, attr_value)

    def __eq__(self, other):
        # This is required for python 2 support so that MapAttribute can call this method.
        return self is other

    def __ne__(self, other):
        # This is required for python 2 support so that MapAttribute can call this method.
        return self is not other


class SetMixin(object):
    """
    Adds (de)serialization methods for sets
    """
    def serialize(self, value):
        """
        Serializes a set

        Because dynamodb doesn't store empty attributes,
        empty sets return None
        """
        if value is not None:
            try:
                iter(value)
            except TypeError:
                value = [value]
            if len(value):
                return [json.dumps(val) for val in sorted(value)]
        return None

    def deserialize(self, value):
        """
        Deserializes a set
        """
        if value and len(value):
            return {json.loads(val) for val in value}


class BinaryAttribute(Attribute):
    """
    A binary attribute
    """
    attr_type = BINARY

    def serialize(self, value):
        """
        Returns a base64 encoded binary string
        """
        return b64encode(value).decode(DEFAULT_ENCODING)

    def deserialize(self, value):
        """
        Returns a decoded string from base64
        """
        try:
            return b64decode(value.decode(DEFAULT_ENCODING))
        except AttributeError:
            return b64decode(value)


class BinarySetAttribute(SetMixin, Attribute):
    """
    A binary set
    """
    attr_type = BINARY_SET
    null = True

    def serialize(self, value):
        """
        Returns a base64 encoded binary string
        """
        if value and len(value):
            return [b64encode(val).decode(DEFAULT_ENCODING) for val in sorted(value)]
        else:
            return None

    def deserialize(self, value):
        """
        Returns a decoded string from base64
        """
        try:
            if value and len(value):
                return {b64decode(val.decode(DEFAULT_ENCODING)) for val in value}
        except AttributeError:
            return {b64decode(val) for val in value}


class UnicodeSetAttribute(SetMixin, Attribute):
    """
    A unicode set
    """
    attr_type = STRING_SET
    null = True

    def element_serialize(self, value):
        """
        This serializes unicode / strings out as unicode strings.
        It does not touch the value if it is already a unicode str
        :param value:
        :return:
        """
        if isinstance(value, six.text_type):
            return value
        return six.u(str(value))

    def element_deserialize(self, value):
        return value

    def serialize(self, value):
        if value is not None:
            try:
                iter(value)
            except TypeError:
                value = [value]
            if len(value):
                return [self.element_serialize(val) for val in sorted(value)]
        return None

    def deserialize(self, value):
        if value and len(value):
            return {self.element_deserialize(val) for val in value}


class UnicodeAttribute(Attribute):
    """
    A unicode attribute
    """
    attr_type = STRING

    def serialize(self, value):
        """
        Returns a unicode string
        """
        if value is None or not len(value):
            return None
        elif isinstance(value, six.text_type):
            return value
        else:
            return six.u(value)


class JSONAttribute(Attribute):
    """
    A JSON Attribute

    Encodes JSON to unicode internally
    """
    attr_type = STRING

    def serialize(self, value):
        """
        Serializes JSON to unicode
        """
        if value is None:
            return None
        encoded = json.dumps(value)
        try:
            return unicode(encoded)
        except NameError:
            return encoded

    def deserialize(self, value):
        """
        Deserializes JSON
        """
        return json.loads(value, strict=False)


class BooleanAttribute(Attribute):
    """
    A class for boolean attributes
    """
    attr_type = BOOLEAN

    def serialize(self, value):
        if value is None:
            return None
        elif value:
            return True
        else:
            return False

    def deserialize(self, value):
        return bool(value)


class NumberSetAttribute(SetMixin, Attribute):
    """
    A number set attribute
    """
    attr_type = NUMBER_SET
    null = True


class NumberAttribute(Attribute):
    """
    A number attribute
    """
    attr_type = NUMBER

    def serialize(self, value):
        """
        Encode numbers as JSON
        """
        return json.dumps(value)

    def deserialize(self, value):
        """
        Decode numbers from JSON
        """
        return json.loads(value)


class VersionAttribute(NumberAttribute):
    """
    A version attribute
    """
    null = True

    def __set__(self, instance, value):
        """
        Cast assigned value to int.
        """
        super(VersionAttribute, self).__set__(instance, int(value))

    def __get__(self, instance, owner):
        """
        Cast retrieved value to int.
        """
        val = super(VersionAttribute, self).__get__(instance, owner)
        return int(val) if isinstance(val, float) else val

    def serialize(self, value):
        """
        Cast value to int then encode as JSON
        """
        return super(VersionAttribute, self).serialize(int(value))

    def deserialize(self, value):
        """
        Decode numbers from JSON and cast to int.
        """
        return int(super(VersionAttribute, self).deserialize(value))


class TTLAttribute(Attribute):
    """
    A time-to-live attribute that signifies when the item expires and can be automatically deleted.
    It can be assigned with a timezone-aware datetime value (for absolute expiry time)
    or a timedelta value (for expiry relative to the current time),
    but always reads as a UTC datetime value.
    """
    attr_type = NUMBER

    def _normalize(self, value):
        """
        Converts value to a UTC datetime
        """
        if value is None:
            return
        if isinstance(value, timedelta):
            value = int(time.time() + value.total_seconds())
        elif isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValueError("datetime must be timezone-aware")
            value = calendar.timegm(value.utctimetuple())
        else:
            raise ValueError("TTLAttribute value must be a timedelta or datetime")
        return datetime.utcfromtimestamp(value).replace(tzinfo=tzutc())

    def __set__(self, instance, value):
        """
        Converts assigned values to a UTC datetime
        """
        super(TTLAttribute, self).__set__(instance, self._normalize(value))

    def serialize(self, value):
        """
        Serializes a datetime as a timestamp (Unix time).
        """
        if value is None:
            return None
        return json.dumps(calendar.timegm(self._normalize(value).utctimetuple()))

    def deserialize(self, value):
        """
        Deserializes a timestamp (Unix time) as a UTC datetime.
        """
        timestamp = json.loads(value)
        return datetime.utcfromtimestamp(timestamp).replace(tzinfo=tzutc())


class UTCDateTimeAttribute(Attribute):
    """
    An attribute for storing a UTC Datetime
    """
    attr_type = STRING

    def serialize(self, value):
        """
        Takes a datetime object and returns a string
        """
        if value.tzinfo is None:
            value = value.replace(tzinfo=tzutc())
        fmt = value.astimezone(tzutc()).strftime(DATETIME_FORMAT)
        return six.u(fmt)

    def deserialize(self, value):
        """
        Takes a UTC datetime string and returns a datetime object
        """
        try:
            return _fast_parse_utc_datestring(value)
        except (ValueError, IndexError):
            try:
                # Attempt to parse the datetime with the datetime format used
                # by default when storing UTCDateTimeAttributes.  This is significantly
                # faster than always going through dateutil.
                return datetime.strptime(value, DATETIME_FORMAT)
            except ValueError:
                return parse(value)


class NullAttribute(Attribute):
    attr_type = NULL

    def serialize(self, value):
        return True

    def deserialize(self, value):
        return None


class MapAttribute(Attribute, AttributeContainer):
    """
    A Map Attribute

    The MapAttribute class can be used to store a JSON document as "raw" name-value pairs, or
    it can be subclassed and the document fields represented as class attributes using Attribute instances.

    To support the ability to subclass MapAttribute and use it as an AttributeContainer, instances of
    MapAttribute behave differently based both on where they are instantiated and on their type.
    Because of this complicated behavior, a bit of an introduction is warranted.

    Models that contain a MapAttribute define its properties using a class attribute on the model.
    For example, below we define "MyModel" which contains a MapAttribute "my_map":

    class MyModel(Model):
       my_map = MapAttribute(attr_name="dynamo_name", default={})

    When instantiated in this manner (as a class attribute of an AttributeContainer class), the MapAttribute
    class acts as an instance of the Attribute class. The instance stores data about the attribute (in this
    example the dynamo name and default value), and acts as a data descriptor, storing any value bound to it
    on the `attribute_values` dictionary of the containing instance (in this case an instance of MyModel).

    Unlike other Attribute types, the value that gets bound to the containing instance is a new instance of
    MapAttribute, not an instance of the primitive type. For example, a UnicodeAttribute stores strings in
    the `attribute_values` of the containing instance; a MapAttribute does not store a dict but instead stores
    a new instance of itself. This difference in behavior is necessary when subclassing MapAttribute in order
    to access the Attribute data descriptors that represent the document fields.

    For example, below we redefine "MyModel" to use a subclass of MapAttribute as "my_map":

    class MyMapAttribute(MapAttribute):
        my_internal_map = MapAttribute()

    class MyModel(Model):
        my_map = MyMapAttribute(attr_name="dynamo_name", default = {})

    In order to set the value of my_internal_map on an instance of MyModel we need the bound value for "my_map"
    to be an instance of MapAttribute so that it acts as a data descriptor:

    MyModel().my_map.my_internal_map = {'foo': 'bar'}

    That is the attribute access of "my_map" must return a MyMapAttribute instance and not a dict.

    When an instance is used in this manner (bound to an instance of an AttributeContainer class),
    the MapAttribute class acts as an AttributeContainer class itself. The instance does not store data
    about the attribute, and does not act as a data descriptor. The instance stores name-value pairs in its
    internal `attribute_values` dictionary.

    Thus while MapAttribute multiply inherits from Attribute and AttributeContainer, a MapAttribute instance
    does not behave as both an Attribute AND an AttributeContainer. Rather an instance of MapAttribute behaves
    EITHER as an Attribute OR as an AttributeContainer, depending on where it was instantiated.

    So, how do we create this dichotomous behavior? Using the AttributeContainerMeta metaclass.
    All MapAttribute instances are initialized as AttributeContainers only. During construction of
    AttributeContainer classes (subclasses of MapAttribute and Model), any instances that are class attributes
    are transformed from AttributeContainers to Attributes (via the `_make_attribute` method call).
    """
    attr_type = MAP

    attribute_args = getfullargspec(Attribute.__init__).args[1:]

    def __init__(self, **attributes):
        # Store the kwargs used by Attribute.__init__ in case `_make_attribute` is called.
        self.attribute_kwargs = {arg: attributes.pop(arg) for arg in self.attribute_args if arg in attributes}

        # Assume all instances should behave like an AttributeContainer. Instances that are intended to be
        # used as Attributes will be transformed by AttributeContainerMeta during creation of the containing class.
        # Because of this do not use MRO or cooperative multiple inheritance, call the parent class directly.
        AttributeContainer.__init__(self, **attributes)

        # It is possible that attributes names can collide with argument names of Attribute.__init__.
        # Assume that this is the case if any of the following are true:
        #   - the user passed in other attributes that did not match any argument names
        #   - this is a "raw" (i.e. non-subclassed) MapAttribute instance and attempting to store the attributes
        #     cannot raise a ValueError (if this assumption is wrong, calling `_make_attribute` removes them)
        #   - the names of all attributes in self.attribute_kwargs match attributes defined on the class
        if self.attribute_kwargs and (
                attributes or self.is_raw() or all(arg in self.get_attributes() for arg in self.attribute_kwargs)):
            self._set_attributes(**self.attribute_kwargs)

    def _is_attribute_container(self):
        # Determine if this instance is being used as an AttributeContainer or an Attribute.
        # AttributeContainer instances have an internal `attribute_values` dictionary that is removed
        # by the `_make_attribute` call during initialization of the containing class.
        return 'attribute_values' in self.__dict__

    def _make_attribute(self):
        # WARNING! This function is only intended to be called from the AttributeContainerMeta metaclass.
        if not self._is_attribute_container():
            # This instance has already been initialized by another AttributeContainer class.
            return False
        # During initialization the kwargs were stored in `attribute_kwargs`. Remove them and re-initialize the class.
        kwargs = self.attribute_kwargs
        del self.attribute_kwargs
        del self.attribute_values
        Attribute.__init__(self, **kwargs)
        for name, attr in self.get_attributes().items():
            # Set a local attribute with the same name that shadows the class attribute.
            # Because attr is a data descriptor and the attribute already exists on the class,
            # we have to store the local copy directly into __dict__ to prevent calling attr.__set__.
            # Use deepcopy so that `attr_path` and any local attributes are also copied.
            self.__dict__[name] = deepcopy(attr)
        return True

    def _update_attribute_paths(self, path_segment):
        # WARNING! This function is only intended to be called from the AttributeContainerMeta metaclass.
        if self._is_attribute_container():
            raise AssertionError("MapAttribute._update_attribute_paths called before MapAttribute._make_attribute")
        for name in self.get_attributes().keys():
            local_attr = self.__dict__[name]
            local_attr.attr_path.insert(0, path_segment)
            if isinstance(local_attr, MapAttribute):
                local_attr._update_attribute_paths(path_segment)

    def __eq__(self, other):
        if self._is_attribute_container():
            return AttributeContainer.__eq__(self, other)
        return Attribute.__eq__(self, other)

    def __ne__(self, other):
        if self._is_attribute_container():
            return AttributeContainer.__ne__(self, other)
        return Attribute.__ne__(self, other)

    def __iter__(self):
        if self._is_attribute_container():
            return iter(self.attribute_values)
        return super(MapAttribute, self).__iter__()

    def __getitem__(self, item):
        if self._is_attribute_container():
            return self.attribute_values[item]
        # If this instance is being used as an Attribute, treat item access like the map dereference operator.
        # This provides equivalence between DynamoDB's nested attribute access for map elements (MyMap.nestedField)
        # and Python's item access for dictionaries (MyMap['nestedField']).
        if self.is_raw():
            return Path(self.attr_path + [str(item)])
        elif item in self._attributes:
            return getattr(self, item)
        else:
            raise AttributeError("'{}' has no attribute '{}'".format(self.__class__.__name__, item))

    def __setitem__(self, item, value):
        if not self._is_attribute_container():
            raise TypeError("'{}' object does not support item assignment".format(self.__class__.__name__))
        if self.is_raw():
            self.attribute_values[item] = value
        elif item in self._attributes:
            setattr(self, item, value)
        else:
            raise AttributeError("'{}' has no attribute '{}'".format(self.__class__.__name__, item))

    def __getattr__(self, attr):
        # This should only be called for "raw" (i.e. non-subclassed) MapAttribute instances.
        # MapAttribute subclasses should access attributes via the Attribute descriptors.
        if self.is_raw() and self._is_attribute_container():
            try:
                return self.attribute_values[attr]
            except KeyError:
                pass
        raise AttributeError("'{}' has no attribute '{}'".format(self.__class__.__name__, attr))

    def __setattr__(self, name, value):
        # "Raw" (i.e. non-subclassed) instances set their name-value pairs in the `attribute_values` dictionary.
        # MapAttribute subclasses should set attributes via the Attribute descriptors.
        if self.is_raw() and self._is_attribute_container():
            self.attribute_values[name] = value
        else:
            object.__setattr__(self, name, value)

    def __set__(self, instance, value):
        if isinstance(value, collections.Mapping):
            value = type(self)(**value)
        return super(MapAttribute, self).__set__(instance, value)

    def _set_attributes(self, **attrs):
        """
        Sets the attributes for this object
        """
        if self.is_raw():
            for name, value in six.iteritems(attrs):
                setattr(self, name, value)
        else:
            super(MapAttribute, self)._set_attributes(**attrs)

    def is_correctly_typed(self, key, attr):
        can_be_null = attr.null
        value = getattr(self, key)
        if can_be_null and value is None:
            return True
        if getattr(self, key) is None:
            raise ValueError("Attribute '{}' cannot be None".format(key))
        return True  # TODO: check that the actual type of `value` meets requirements of `attr`

    def validate(self):
        return all(self.is_correctly_typed(k, v) for k, v in six.iteritems(self.get_attributes()))

    def serialize(self, values):
        rval = {}
        for k in values:
            v = values[k]
            if self._should_skip(v):
                continue
            attr_class = self._get_serialize_class(k, v)
            if attr_class is None:
                continue
            if attr_class.attr_type:
                attr_key = ATTR_TYPE_MAP[attr_class.attr_type]
            else:
                attr_key = _get_key_for_serialize(v)

            # If this is a subclassed MapAttribute, there may be an alternate attr name
            attr = self.get_attributes().get(k)
            attr_name = attr.attr_name if attr else k

            serialized = attr_class.serialize(v)
            if self._should_skip(serialized):
                # Check after we serialize in case the serialized value is null
                continue

            rval[attr_name] = {attr_key: serialized}

        return rval

    def deserialize(self, values):
        """
        Decode as a dict.
        """
        deserialized_dict = dict()
        for k in values:
            v = values[k]
            attr_value = _get_value_for_deserialize(v)
            key = self._dynamo_to_python_attr(k)
            attr_class = self._get_deserialize_class(key, v)
            if attr_class is None:
                continue
            deserialized_value = None
            if attr_value is not None:
                deserialized_value = attr_class.deserialize(attr_value)

            deserialized_dict[key] = deserialized_value

        # If this is a subclass of a MapAttribute (i.e typed), instantiate an instance
        if not self.is_raw():
            return type(self)(**deserialized_dict)
        return deserialized_dict

    @classmethod
    def is_raw(cls):
        return cls == MapAttribute

    def as_dict(self):
        result = {}
        for key, value in six.iteritems(self.attribute_values):
            result[key] = value.as_dict() if isinstance(value, MapAttribute) else value
        return result

    def _should_skip(self, value):
        # Continue to serialize NULL values in "raw" map attributes for backwards compatibility.
        # This special case behavior for "raw" attribtues should be removed in the future.
        return not self.is_raw() and value is None

    @classmethod
    def _get_serialize_class(cls, key, value):
        if not cls.is_raw():
            return cls.get_attributes().get(key)
        return _get_class_for_serialize(value)

    @classmethod
    def _get_deserialize_class(cls, key, value):
        if not cls.is_raw():
            return cls.get_attributes().get(key)
        return _get_class_for_deserialize(value)


def _get_value_for_deserialize(value):
    key = next(iter(value.keys()))
    if key == NULL:
        return None
    return value[key]


def _get_class_for_deserialize(value):
    value_type = list(value.keys())[0]
    if value_type not in DESERIALIZE_CLASS_MAP:
        raise ValueError('Unknown value: ' + str(value))
    return DESERIALIZE_CLASS_MAP[value_type]


def _get_class_for_serialize(value):
    if value is None:
        return NullAttribute()
    if isinstance(value, MapAttribute):
        return type(value)()
    value_type = type(value)
    if value_type not in SERIALIZE_CLASS_MAP:
        raise ValueError('Unknown value: {}'.format(value_type))
    return SERIALIZE_CLASS_MAP[value_type]


def _get_key_for_serialize(value):
    if value is None:
        return NullAttribute.attr_type
    if isinstance(value, MapAttribute):
        return MAP_SHORT
    value_type = type(value)
    if value_type not in SERIALIZE_KEY_MAP:
        raise ValueError('Unknown value: {}'.format(value_type))
    return SERIALIZE_KEY_MAP[value_type]


def _fast_parse_utc_datestring(datestring):
    # Method to quickly parse strings formatted with '%Y-%m-%dT%H:%M:%S.%f+0000'.
    # This is ~5.8x faster than using strptime and 38x faster than dateutil.parser.parse.
    _int = int  # Hack to prevent global lookups of int, speeds up the function ~10%
    try:
        if (datestring[4] != '-' or datestring[7] != '-' or datestring[10] != 'T' or
                datestring[13] != ':' or datestring[16] != ':' or datestring[19] != '.' or
                datestring[-5:] != '+0000'):
            raise ValueError("Datetime string '{}' does not match format "
                            "'%Y-%m-%dT%H:%M:%S.%f+0000'".format(datestring))
        return datetime(
            _int(datestring[0:4]), _int(datestring[5:7]), _int(datestring[8:10]),
            _int(datestring[11:13]), _int(datestring[14:16]), _int(datestring[17:19]),
            _int(round(float(datestring[19:-5]) * 1e6)), tzutc()
        )
    except (TypeError, ValueError):
        raise ValueError("Datetime string '{}' does not match format "
                         "'%Y-%m-%dT%H:%M:%S.%f+0000'".format(datestring))


class ListAttribute(Attribute):
    attr_type = LIST
    element_type = None

    def __init__(self, hash_key=False, range_key=False, null=None, default=None, attr_name=None, of=None):
        super(ListAttribute, self).__init__(hash_key=hash_key,
                                            range_key=range_key,
                                            null=null,
                                            default=default,
                                            attr_name=attr_name)
        if of:
            if not issubclass(of, MapAttribute):
                raise ValueError("'of' must be subclass of MapAttribute")
            self.element_type = of

    def remove(self, indexes=None):
        if indexes is not None:
            if not isinstance(indexes, list) or not all([isinstance(i, int) for i in indexes]):
                raise ValueError("Method 'remove' argument 'indexes' needs to be a list of integers")
            return Path(self).remove_list_elements(indexes)
        return Path(self).remove()

    def serialize(self, values):
        """
        Encode the given list of objects into a list of AttributeValue types.
        """
        rval = []
        for v in values:
            attr_class = (self.element_type()
                          if self.element_type
                          else _get_class_for_serialize(v))
            if attr_class.attr_type:
                attr_key = ATTR_TYPE_MAP[attr_class.attr_type]
            else:
                attr_key = _get_key_for_serialize(v)
            rval.append({attr_key: attr_class.serialize(v)})
        return rval

    def deserialize(self, values):
        """
        Decode from list of AttributeValue types.
        """
        deserialized_lst = []
        for v in values:
            class_for_deserialize = self.element_type() if self.element_type else _get_class_for_deserialize(v)
            attr_value = _get_value_for_deserialize(v)
            deserialized_lst.append(class_for_deserialize.deserialize(attr_value))
        return deserialized_lst

DESERIALIZE_CLASS_MAP = {
    LIST_SHORT: ListAttribute(),
    NUMBER_SHORT: NumberAttribute(),
    STRING_SHORT: UnicodeAttribute(),
    BOOLEAN: BooleanAttribute(),
    MAP_SHORT: MapAttribute(),
    NULL: NullAttribute()
}

SERIALIZE_CLASS_MAP = {
    dict: MapAttribute(),
    list: ListAttribute(),
    set: ListAttribute(),
    bool: BooleanAttribute(),
    float: NumberAttribute(),
    int: NumberAttribute(),
    str: UnicodeAttribute(),
}


SERIALIZE_KEY_MAP = {
    dict: MAP_SHORT,
    list: LIST_SHORT,
    set: LIST_SHORT,
    bool: BOOLEAN,
    float: NUMBER_SHORT,
    int: NUMBER_SHORT,
    str: STRING_SHORT,
}


if six.PY2:
    SERIALIZE_CLASS_MAP[unicode] = UnicodeAttribute()
    SERIALIZE_CLASS_MAP[long] = NumberAttribute()
    SERIALIZE_KEY_MAP[unicode] = STRING_SHORT
    SERIALIZE_KEY_MAP[long] = NUMBER_SHORT
