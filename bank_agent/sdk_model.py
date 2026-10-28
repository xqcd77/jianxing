"""Agents SDK Model bridge for the existing provider-specific proposal protocol.

The transport owns DeepSeek thinking/JSON settings and bounded repair diagnostics.
The SDK owns typed Agent execution. Neither receives executable banking tools.
"""

from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from .storage import canonical, uid


class ProposalModel(Model):
    def __init__(self, proposal, usage):
        self.proposal, self.provider_usage = proposal, usage

    async def get_response(self, *args, **kwargs):
        result = await self.proposal()

        def count(name):
            value = self.provider_usage.get(name)
            return value if type(value) is int and value >= 0 else 0

        message = ResponseOutputMessage(
            id=uid("proposal"), type="message", role="assistant", status="completed",
            content=[ResponseOutputText(
                type="output_text", annotations=[], text=canonical(result.model_dump(mode="json")),
            )],
        )
        return ModelResponse(
            output=[message], response_id=None,
            usage=Usage(requests=1, input_tokens=count("prompt_tokens"),
                        output_tokens=count("completion_tokens"), total_tokens=count("total_tokens")),
        )

    async def stream_response(self, *args, **kwargs):
        # The service streams workflow progress; proposals must be complete and
        # validated before they can enter a plan. Never execute partial JSON.
        raise NotImplementedError("ProposalModel requires non-streaming typed execution")
        yield  # Make this method an async iterator, as required by Model.
