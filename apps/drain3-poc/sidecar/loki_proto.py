"""Wire-compatible subset of grafana/loki pkg/push/push.proto."""
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory, timestamp_pb2

_file = descriptor_pb2.FileDescriptorProto(name="loki_push_poc.proto", package="poc.loki", syntax="proto3")
_file.dependency.append("google/protobuf/timestamp.proto")


def _message(name, fields):
    msg = _file.message_type.add(name=name)
    for number, field_name, field_type, repeated, type_name in fields:
        f = msg.field.add(name=field_name, number=number, type=field_type,
                          label=3 if repeated else 1)
        if type_name:
            f.type_name = type_name


_message("LabelPair", [(1, "name", 9, False, None), (2, "value", 9, False, None)])
_message("Entry", [(1, "timestamp", 11, False, ".google.protobuf.Timestamp"),
                   (2, "line", 9, False, None),
                   (3, "structured_metadata", 11, True, ".poc.loki.LabelPair")])
_message("Stream", [(1, "labels", 9, False, None),
                    (2, "entries", 11, True, ".poc.loki.Entry")])
_message("PushRequest", [(1, "streams", 11, True, ".poc.loki.Stream")])
_pool = descriptor_pool.Default()
_pool.AddSerializedFile(_file.SerializeToString())
PushRequest = message_factory.GetMessageClass(_pool.FindMessageTypeByName("poc.loki.PushRequest"))
