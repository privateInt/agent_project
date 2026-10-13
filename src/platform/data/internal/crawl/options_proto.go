package crawl

import collectionv1 "data-platform/gen/collection/v1"

func OptionsFromProto(p *collectionv1.CrawlOptions) Options {
	return Options{MaxBytes: p.GetMaxBytes(), TimeoutSeconds: p.GetTimeoutSeconds(), FollowLinks: p.GetFollowLinks(), IncludeAttachments: p.GetIncludeAttachments(), MaxDepth: p.GetMaxDepth(), MaxDocuments: p.GetMaxDocuments(), MaxTotalBytes: p.GetMaxTotalBytes(), RenderJavaScript: p.GetRenderJavascript(), RenderWaitMS: p.GetRenderWaitMs()}
}
func (o Options) Proto() *collectionv1.CrawlOptions {
	return &collectionv1.CrawlOptions{MaxBytes: o.MaxBytes, TimeoutSeconds: o.TimeoutSeconds, FollowLinks: o.FollowLinks, IncludeAttachments: o.IncludeAttachments, MaxDepth: o.MaxDepth, MaxDocuments: o.MaxDocuments, MaxTotalBytes: o.MaxTotalBytes, RenderJavascript: o.RenderJavaScript, RenderWaitMs: o.RenderWaitMS}
}

const BrowserProcessorVersion = "chromium-dom-v1"

func (o Options) ExecutionType() string {
	if o.RenderJavaScript {
		return "collection.browser"
	}
	return "collection.crawl"
}
func (o Options) BatchProcessor() string {
	if o.RenderJavaScript {
		return BrowserProcessorVersion
	}
	return ScopeProcessorVersion
}
func (o Options) ArtifactMultiplier() int64 {
	if o.RenderJavaScript {
		return 2
	}
	return 1
}
