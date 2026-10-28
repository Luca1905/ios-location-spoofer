#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SDK_NAME="iphoneos"
TARGET="arm64-apple-ios18.0"
if [[ "${1:-}" == "--simulator" ]]; then
    SDK_NAME="iphonesimulator"
    TARGET="arm64-apple-ios18.0-simulator"
fi
IOS_SDK=$(xcrun --sdk "$SDK_NAME" --show-sdk-path)
IOS_ARCH="arm64"
MIN_IOS_VERSION="18.0"
BUILD_DIR="$SCRIPT_DIR/build"
if [[ "$SDK_NAME" == "iphonesimulator" ]]; then
    BUILD_DIR="$BUILD_DIR/iphonesimulator"
fi

echo "=== Building Go Location Spoofer for iOS ==="
echo "SDK: $IOS_SDK"
echo "Architecture: $IOS_ARCH"
echo "Min iOS Version: $MIN_IOS_VERSION"

# Create build directory
mkdir -p "$BUILD_DIR"

# Set up Go environment for iOS cross-compilation
export CGO_ENABLED=1
export CGO_CFLAGS="-target $TARGET -isysroot $IOS_SDK"
export CGO_LDFLAGS="-target $TARGET -isysroot $IOS_SDK"
export GOOS="ios"
export GOARCH="arm64"
export GO111MODULE="on"

# Build the Go library as a C archive for iOS
# Strip symbols to reduce binary size (important for iOS 50MB memory limit)
echo "Building libgolocationspoofer.a..."
go build -ldflags="-s -w" -buildmode=c-archive \
    -o "$BUILD_DIR/libgolocationspoofer.a" \
    -tags=ios \
    .

# Copy header to project root with proper name
if [[ "$SDK_NAME" == "iphoneos" ]]; then
    cp "$BUILD_DIR/libgolocationspoofer.h" "$SCRIPT_DIR/golocationspoofer.h"
fi
echo "Generated golocationspoofer.h"

# Create symbolic link
if [[ "$SDK_NAME" == "iphoneos" ]]; then
    ln -sf "$BUILD_DIR/libgolocationspoofer.a" "$SCRIPT_DIR/libgolocationspoofer.a"
fi

echo ""
echo "=== Build Complete ==="
echo "Static library: $BUILD_DIR/libgolocationspoofer.a"
echo "Header file: $SCRIPT_DIR/golocationspoofer.h"
echo ""
echo "To use in Xcode:"
echo "1. Add libgolocationspoofer.a to 'Linked Frameworks and Libraries'"
echo "2. Add golocationspoofer.h to your project"
echo "3. Add to 'Other Linker Flags': -l golocationspoofer"
echo "4. Add to 'Header Search Paths': $SCRIPT_DIR"