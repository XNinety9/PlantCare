from typing import Literal

from pydantic import BaseModel, Field

LANGUAGES = ("en", "fr")


class CareSheet(BaseModel):
    """Données de soin indépendantes de la langue, affichées en jauges et en badges."""

    difficulty: Literal["easy", "medium", "hard"] = Field(description="Indoor care difficulty")
    light_level: Literal["low", "medium", "bright_indirect", "direct_sun"] = Field(description="Light needs")
    watering_interval_days: int = Field(description="Average days between waterings during the growing season (spring and summer)")
    watering_interval_winter_days: int = Field(description="Average days between waterings during winter rest")
    fertilize_interval_days: int = Field(description="Days between feeds during the growing season, 0 if the plant should not be fed")
    repot_interval_months: int = Field(description="Months between repottings for an adult plant")
    humidity_level: Literal["low", "medium", "high"] = Field(description="Ambient humidity needs")
    temperature_min_c: int = Field(description="Minimum tolerated temperature, in °C")
    temperature_max_c: int = Field(description="Maximum ideal temperature, in °C")
    pot_size_cm: int = Field(description="Recommended pot diameter for an adult indoor plant, in cm")
    pet_safe: bool = Field(description="True if the plant is safe for cats and dogs")


class CareTexts(BaseModel):
    common_name: str = Field(description="Short usual common name (e.g. 'Maranta'), no parentheses or alternatives")
    summary: str = Field(description="One sentence (20 words max) introducing the plant and its character")
    light: str = Field(description="Ideal placement, 8 words max (e.g. '1-2 m from an east window')")
    watering: str = Field(description="When and how to water, 12 words max")
    humidity: str = Field(description="Ideal humidity and a tip, 10 words max")
    pot: str = Field(description="Pot and repotting advice, 10 words max")
    substrate: str = Field(description="Ideal potting mix, 10 words max")
    fertilizer: str = Field(description="Fertilizer type and schedule, 10 words max")
    toxicity: str = Field(description="Toxicity for humans and pets, 8 words max")
    tips: list[str] = Field(description="3 to 5 tips or quirks, 15 words max each")


class Texts(BaseModel):
    en: CareTexts = Field(description="Texts in English")
    fr: CareTexts = Field(description="Les mêmes textes, en français")


class PlantSheet(BaseModel):
    """Ce qui est stocké en base pour chaque plante."""

    care: CareSheet
    texts: Texts


class PlantIdentification(BaseModel):
    is_plant: bool = Field(description="False if the photo does not show a plant")
    scientific_name: str = Field(description="Scientific name (genus, species and cultivar if recognized), empty if unidentified")
    confidence: float = Field(description="Confidence in the identification, between 0 and 1")
    care: CareSheet | None = Field(description="Care data, null if the photo does not show a plant")
    texts: Texts | None = Field(description="Care texts in English and French, null if the photo does not show a plant")


class DiagnosisTexts(BaseModel):
    title: str = Field(description="What you see, 8 words max (e.g. 'Brown, crispy leaf edges')")
    causes: list[str] = Field(description="1 to 3 likely causes, most likely first, 12 words max each")
    actions: list[str] = Field(description="2 to 4 concrete actions to take, 15 words max each")


class DiagnosisTextsByLanguage(BaseModel):
    en: DiagnosisTexts = Field(description="Texts in English")
    fr: DiagnosisTexts = Field(description="Les mêmes textes, en français")


class Diagnosis(BaseModel):
    shows_plant: bool = Field(description="False if the photo does not show a plant or a part of a plant")
    severity: Literal["healthy", "watch", "urgent"] = Field(description="healthy: nothing to worry about; watch: minor issue; urgent: act now")
    texts: DiagnosisTextsByLanguage | None = Field(description="Diagnosis texts, null if the photo does not show a plant")
