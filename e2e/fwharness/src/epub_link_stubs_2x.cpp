// Link-time stand-ins for CrossPoint 2.x firmware pieces the host EPUB harness does not
// exercise: image decoders (articles carry no images) and the content-protection backend.
#include <ContentProtection.h>
#include <Epub/Page.h>
#include <Epub/TokenBoundary.h>
#include <Epub/blocks/ImageBlock.h>
#include <Epub/blocks/TextBlock.h>
#include <Epub/converters/ImageDecoderFactory.h>
#include <Epub/converters/ImageToFramebufferDecoder.h>
#include <Epub/hyphenation/Hyphenator.h>
#include <GfxRenderer.h>
#include <BidiUtils.h>
#include <ZipFile.h>

ImageBlock::ImageBlock(const std::string& imagePath, const std::string& srcPath, int16_t width, int16_t height)
    : imagePath(imagePath), srcPath(srcPath), width(width), height(height) {}
bool ImageDecoderFactory::isFormatSupported(const std::string&) { return false; }
ImageToFramebufferDecoder* ImageDecoderFactory::getDecoder(const std::string&) { return nullptr; }
bool ImageToFramebufferDecoder::validateAndStoreDimensions(int64_t, int64_t, ImageDimensions&, const char*) { return false; }
void ImageBlock::render(GfxRenderer&, int, int) {}
void ImageBlock::renderPlaceholder(GfxRenderer&, int, int) const {}
bool ImageBlock::needsDecode() const { return false; }
bool ImageBlock::serialize(HalFile&) { return false; }
std::unique_ptr<ImageBlock> ImageBlock::deserialize(HalFile&) { return nullptr; }

namespace freeink {
namespace content {
// Same first step as lib/Epub/ContentProtection.cpp: a book without
// META-INF/encryption.xml uses plain ZIP reads (nullptr, no error). Encrypted books are
// out of scope for the harness and reported as an error so they cannot pass silently.
std::unique_ptr<ContentDecryptor> openProtectedBook(const std::string& epubPath, std::string& err) {
  err.clear();
  size_t manifestSize = 0;
  if (!ZipFile(epubPath).getInflatedFileSize("META-INF/encryption.xml", &manifestSize)) return nullptr;
  err = "harness: META-INF/encryption.xml present (encrypted EPUBs are not supported)";
  return nullptr;
}
}  // namespace content
}  // namespace freeink
