package testsupport

import (
	"strings"
	rerankingv1 "knowledge-platform/gen/model/reranking/v1"
)

func RerankerProfile()*rerankingv1.RerankerProfile{
	id:="99999999-9999-4999-8999-999999999999"
	return &rerankingv1.RerankerProfile{
		DeploymentId:id,ModelVersionId:"88888888-8888-4888-8888-888888888888",ProfileId:"ko-reranker-v1",ServingProfile:"ko-reranker-v1",
		RerankerProfileId:"rp_"+strings.Repeat("a",64),SourceManifestSha256:strings.Repeat("b",64),TokenizerSha256:strings.Repeat("c",64),
		RuntimeImageId:"sha256:"+strings.Repeat("d",64),RuntimeContainerId:strings.Repeat("e",64),ServedModelName:"deployment-"+id,
		RepoId:"Dongjin-kr/ko-reranker",ResolvedRevision:strings.Repeat("f",40),RegisteredModelType:"llm",ScoreKind:"logit",ScoreDirection:"higher_is_better",
		PairPolicy:"xlmr-pair-special4-no-prefix-v1",MaxInputTokens:512,MaxCandidates:100,MaxInputBytes:131072,Truncation:"reject",Capability:"reranker",Status:"ready",
	}
}
