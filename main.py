import cv2

face_c = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
eye_c = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_eye.xml")

CLOSED_FRAMES = 2  # consecutive frames without eyes to count as a blink


def main():
    cap = cv2.VideoCapture(0)
    blinks, closed = 0, 0
    while cap.isOpened():
        ok, frame = cap.read()
        if not ok:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        for x, y, w, h in face_c.detectMultiScale(gray, 1.3, 5)[:1]:
            cv2.rectangle(frame, (x, y), (x + w, y + h), (255, 0, 0), 2)
            roi = gray[y:y + h // 2, x:x + w]  # eyes live in the upper half of the face
            eyes = eye_c.detectMultiScale(roi, 1.1, 8)
            for ex, ey, ew, eh in eyes:
                cv2.rectangle(frame, (x + ex, y + ey), (x + ex + ew, y + ey + eh), (0, 255, 0), 2)
            if len(eyes) == 0:
                closed += 1
            else:
                if closed >= CLOSED_FRAMES:
                    blinks += 1
                closed = 0
        cv2.putText(frame, f"Blinks: {blinks}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
        cv2.imshow("Eye Blink Detection", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break
    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
