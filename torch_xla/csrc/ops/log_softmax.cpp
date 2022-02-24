#include "torch_xla/csrc/ops/log_softmax.h"

#include "torch/csrc/lazy/core/ir.h"
#include "torch/csrc/lazy/core/tensor_util.h"
#include "torch_xla/csrc/convert_ops.h"
#include "torch_xla/csrc/lowering_context.h"
#include "torch_xla/csrc/softmax_builder.h"
#include "torch_xla/csrc/tensor_util.h"
#include "torch_xla/csrc/torch_util.h"

namespace torch_xla {
namespace ir {
namespace ops {
namespace {

xla::XlaOp LowerLogSoftmax(xla::XlaOp input, int64_t dim,
                           const c10::optional<at::ScalarType>& dtype) {
  xla::XlaOp result = BuildLogSoftmax(input, dim);
  return CastToScalarType(result, dtype);
}

xla::Shape NodeOutputShape(const Value& input,
                           const c10::optional<at::ScalarType>& dtype) {
  if (dtype) {
    return xla::ShapeUtil::ChangeElementType(
        input.shape(), MakeXlaPrimitiveType(*dtype, /*device=*/nullptr));
  }
  return input.shape();
}

}  // namespace

LogSoftmax::LogSoftmax(const Value& input, int64_t dim,
                       c10::optional<at::ScalarType> dtype)
    : Node(torch::lazy::OpKind(at::aten::log_softmax), {input},
           [&]() { return NodeOutputShape(input, dtype); },
           /*num_outputs=*/1,
           torch::lazy::MHash(dim, torch::lazy::OptionalOr<int>(dtype, -1))),
      dim_(dim),
      dtype_(dtype) {}

NodePtr LogSoftmax::Clone(OpList operands) const {
  return MakeNode<LogSoftmax>(operands.at(0), dim_, dtype_);
}

XlaOpVector LogSoftmax::Lower(LoweringContext* loctx) const {
  xla::XlaOp input = loctx->GetOutputOp(operand_with_shape(0));
  return ReturnOp(LowerLogSoftmax(input, dim_, dtype_), loctx);
}

std::string LogSoftmax::ToString() const {
  std::stringstream ss;
  ss << Node::ToString() << ", dim=" << dim_
     << ", dtype=" << torch::lazy::OptionalOr<int>(dtype_, -1);
  return ss.str();
}

}  // namespace ops
}  // namespace ir
}  // namespace torch_xla
