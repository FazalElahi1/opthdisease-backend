import os
import json
import httpx
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_URL     = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

# ── Eye disease keyword whitelist for question restriction ────────────────────
EYE_KEYWORDS = [
    "eye", "vision", "retina", "retinal", "optic", "cornea", "lens", "pupil",
    "iris", "macula", "vitreous", "glaucoma", "cataract", "diabetic retinopathy",
    "amd", "macular degeneration", "myopia", "hyperopia", "astigmatism",
    "presbyopia", "conjunctivitis", "uveitis", "fundus", "intraocular",
    "ophthalmology", "optometry", "visual acuity", "blind", "sight",
    "floaters", "flashes", "blurry", "double vision", "dry eye",
    "hypertensive retinopathy", "blood vessel", "haemorrhage", "exudate",
    "drusen", "scan", "heatmap", "xai", "report", "finding", "risk",
    "treatment", "medicine", "drops", "surgery", "laser", "injection",
    "diagnosis", "symptom", "prevent", "screen", "check",
]


async def _call_gemini(prompt: str, image_base64: str = None) -> str:
    """Core Gemini API call."""
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY not set in .env")

    parts = []

    if image_base64:
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data":      image_base64,
            }
        })

    parts.append({"text": prompt})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature":     0.3,
            "maxOutputTokens": 1024,
            # Disable Gemini 2.5 "thinking" mode — we want fast, direct output
            # (JSON for image analysis, plain prose for recommendations) with no
            # reasoning preamble and no extra latency.
            "thinkingConfig":  {"thinkingBudget": 0},
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(
            f"{GEMINI_URL}?key={GEMINI_API_KEY}",
            json=payload,
        )

    if res.status_code != 200:
        raise ValueError(f"Gemini API error {res.status_code}: {res.text}")

    data = res.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


async def is_eye_related_question(question: str) -> bool:
    """
    Fast local check first (keyword match), then Gemini for borderline cases.
    Returns True only if the question is about eye health / ophthalmology.
    """
    q_lower = question.lower()

    # Fast path: keyword match
    if any(kw in q_lower for kw in EYE_KEYWORDS):
        return True

    # Slow path: ask Gemini for borderline questions
    prompt = f"""You are a medical topic classifier.
Is the following question related to eye health, vision, or ophthalmology?
Answer with only a single word: YES or NO.

Question: {question}"""

    try:
        answer = await _call_gemini(prompt)
        return "yes" in answer.strip().lower()
    except Exception:
        # If Gemini fails, default to allowing the question
        return True


async def analyze_eye_image_with_restriction(
    image_base64:   str,
    blood_pressure: Optional[str] = None,
    sugar_level:    Optional[str] = None,
    clinical_info:  Optional[str] = None,
) -> dict:
    """
    Analyzes a fundus image for eye diseases.
    Strictly restricted to ophthalmological findings only.
    Returns structured JSON with riskLevel, findings, primaryCondition.
    """
    clinical_context = ""
    if blood_pressure:
        clinical_context += f"Blood pressure: {blood_pressure}. "
    if sugar_level:
        clinical_context += f"Blood sugar: {sugar_level}. "
    if clinical_info:
        clinical_context += f"Reported symptoms: {clinical_info}."

    prompt = f"""You are an expert ophthalmologist AI assistant analyzing a fundus retinal image.

STRICT RESTRICTION: You must ONLY analyze and comment on eye-related findings.
Do NOT provide any general medical advice, diagnoses for non-eye conditions,
or recommendations outside of ophthalmology. If asked about anything other than
eye health, respond only about the eye findings.

Clinical context: {clinical_context if clinical_context else "None provided."}

Analyze this fundus image and provide ONLY:
1. Primary eye condition detected (one of: Normal, Diabetic Retinopathy, Glaucoma, Cataract, Age-related Macular Degeneration, Hypertensive Retinopathy, Pathological Myopia, Other)
2. Risk level (Low, Medium, or High)
3. Detailed ophthalmic findings (retinal vessels, optic disc, macula, lesions, haemorrhages, exudates, drusen etc.)
4. Confidence percentage (0-100)

Respond ONLY in this exact JSON format, no other text:
{{
  "primaryCondition": "condition name",
  "riskLevel": "Low|Medium|High",
  "findings": "detailed ophthalmological findings here",
  "confidence": 85,
  "affectedRegions": ["optic disc", "macula", "peripheral retina"],
  "urgency": "Routine|Soon|Urgent"
}}"""

    try:
        raw     = await _call_gemini(prompt, image_base64)
        # Strip markdown code fences if present
        cleaned = raw.strip().replace("```json", "").replace("```", "").strip()
        result  = json.loads(cleaned)
        return result
    except json.JSONDecodeError:
        # Fallback if Gemini returns non-JSON
        return {
            "primaryCondition": "Unable to determine",
            "riskLevel":        "Low",
            "findings":         raw if 'raw' in dir() else "Analysis failed. Please retake the image.",
            "confidence":       0,
            "affectedRegions":  [],
            "urgency":          "Routine",
        }
    except Exception as e:
        raise ValueError(f"Image analysis failed: {str(e)}")


async def generate_personalized_recommendation(
    disease:        str,
    risk_level:     str,
    findings:       str,
    blood_pressure: Optional[str] = None,
    sugar_level:    Optional[str] = None,
    patient_age:    Optional[int] = None,
    is_question:    bool = False,
    scan_context:   str = "",
) -> str:
    """
    Generates a personalized recommendation specific to this patient's
    profile, age, clinical values, and scan findings.

    STRICT: Only ophthalmological recommendations — no general medicine.
    """
    age_str = f"Patient age: {patient_age}." if patient_age else ""
    bp_str  = f"Blood pressure: {blood_pressure}." if blood_pressure else ""
    bg_str  = f"Blood sugar: {sugar_level}." if sugar_level else ""

    if is_question:
        prompt = f"""You are an ophthalmologist AI assistant.
STRICT RESTRICTION: Answer ONLY questions about eye health, vision, and ophthalmology.
If this question is not about eye health, say: "I can only answer eye health questions."

{age_str} {bp_str} {bg_str}
{scan_context}

Patient question: {findings}

Provide a clear, personalized answer in 3-4 sentences. Use simple language.
Focus ONLY on eye health aspects."""

    else:
        prompt = f"""You are an expert ophthalmologist providing a personalized recommendation.
STRICT RESTRICTION: Provide ONLY ophthalmological recommendations.
Do NOT recommend general lifestyle changes unless directly related to eye health.

Patient profile:
- {age_str}
- {bp_str}
- {bg_str}
- Diagnosed condition: {disease}
- Risk level: {risk_level}
- Eye findings: {findings}

Provide a personalized recommendation covering:
1. What this diagnosis means for THIS specific patient given their age and clinical values
2. Specific eye care steps they should take (medications, eye drops, lifestyle changes for eye health)
3. Follow-up schedule (how often to see an ophthalmologist)
4. Warning signs to watch for that require immediate attention
5. Whether they need specialist referral

Keep it under 200 words. Use clear, non-technical language.
Be specific to their profile — not generic advice."""

    try:
        return await _call_gemini(prompt)
    except Exception as e:
        return f"Unable to generate recommendation at this time. Please consult an ophthalmologist. Error: {str(e)}"