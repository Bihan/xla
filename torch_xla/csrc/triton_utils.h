#ifndef TORCH_XLA_CSRC_TRITON_UTILS_H_
#define TORCH_XLA_CSRC_TRITON_UTILS_H_

#include <string>

#include "absl/status/status.h"
#include "absl/status/statusor.h"
#include "absl/strings/string_view.h"
#include "torch_xla/csrc/gpu_vendor.h"

namespace torch_xla::XLA_GPU_NAMESPACE {

absl::StatusOr<std::string> ZlibUncompress(absl::string_view compressed);
absl::StatusOr<std::string> GetTritonKernelCallName(absl::string_view opaque);
absl::StatusOr<std::string> GetTritonKernelCallSerializedMetadata(
    absl::string_view opaque);

}  // namespace torch_xla::XLA_GPU_NAMESPACE

#endif  // TORCH_XLA_CSRC_TRITON_UTILS_H_