#!/bin/bash -eu
# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
################################################################################

# 设置正确的GOPATH环境变量
export GOPATH=/root/go

# 复制fuzzer文件到正确的位置
cp $SRC/schema_fuzzer.go $SRC/pulumi/pkg/codegen/schema/
cp $SRC/config_fuzzer.go $SRC/pulumi/sdk/go/common/resource/config/

# 进入pulumi项目目录
cd $SRC/pulumi

# 由于pulumi项目使用多模块结构，我们需要在每个模块目录中分别构建

# 构建pkg模块的fuzzer
cd pkg
go mod tidy
compile_go_fuzzer ./codegen/schema SchemaFuzzer schema_fuzzer

# 返回pulumi根目录
cd ..

# 构建sdk模块的fuzzers
cd sdk
go mod tidy
compile_go_fuzzer ./go/common/resource/config FuzzConfig fuzz
compile_go_fuzzer ./go/common/resource/config FuzzParseKey fuzz_parse_key