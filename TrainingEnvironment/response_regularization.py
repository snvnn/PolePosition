"""Training-only response dropout in standardized units; inference stays unchanged."""
import math


def response_dropout(batch, generator, settings):
    import torch
    probability=settings['probability']
    columns=settings.get('columns',[24,25])
    if (not math.isfinite(probability) or not 0<=probability<=1 or not columns
            or len(set(columns))!=len(columns) or any(i not in (24,25) for i in columns)):
        raise ValueError('Invalid lateral response dropout settings')
    result=batch.clone()
    selected=torch.rand((len(batch),),generator=generator)<probability
    for column in columns:result[selected,column]=0
    return result
