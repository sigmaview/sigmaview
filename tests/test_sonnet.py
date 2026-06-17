import anthropic
import sys

MODEL = "claude-sonnet-4-6"

def test_connection():
    client = anthropic.Anthropic(base_url="https://api.anthropic.com")

    print(f"Probando conexión con {MODEL}...")

    response = client.messages.create(
        model=MODEL,
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": (
                "Responde solo esto: ¿Cuántas ondas tiene una pauta de impulso "
                "según Elliott Wave? (una línea, sin explicación)"
            )
        }]
    )

    text = response.content[0].text
    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens
    cost_usd = (input_tokens * 3 + output_tokens * 15) / 1_000_000

    print(f"Modelo:          {response.model}")
    print(f"Respuesta:       {text}")
    print(f"Tokens entrada:  {input_tokens}")
    print(f"Tokens salida:   {output_tokens}")
    print(f"Costo estimado:  ${cost_usd:.6f} USD")
    print()
    print("OK — Sonnet operativo")

if __name__ == "__main__":
    try:
        test_connection()
    except anthropic.AuthenticationError:
        print("ERROR: API key inválida o no encontrada")
        print("Corre: ANTHROPIC_API_KEY='sk-ant-...' python3 tests/test_sonnet.py")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)
